"""搜索规则（design-v1.1 §8.5）：关键词 + 语义条件 + 达标分 + 语言多选 + 时间窗 + 回复方式。

搜索结果不在本页显示——每条抓到的推文（含未达标的及原因）都在「抓取记录」页，本页每条规则
都有「查看结果」直达按钮，运行完也会弹出带跳转按钮的结果框。支持「AI 生成规则」：用大白话描述想找谁。
"""
from __future__ import annotations

from nicegui import run, ui

from .. import config
from ..core.matcher import REPLY_MODE_LABEL
from ..core.search import LANG_LABEL, effective_query, langs_label, rule_langs
from ..db.database import get_conn
from .layout import confirm, fmt_time, fmt_views, run_job_with_progress, shell
from .pickers import reply_mode_fields, reply_mode_invalid

_LANG_OPTIONS = {k: v for k, v in LANG_LABEL.items()}

# 各参数的说明与推荐值（弹窗里逐项显示）
HINTS = {
    "keywords": "最简单：逗号隔开多个词，命中任意一个即可（中文词会自动整词匹配）。高级：直接写 X 语法，空格=同时包含、OR=或、-词=排除。"
                "语言不用写，下面勾选；转推默认排除。",
    "semantic": "写给打分 AI 看的：要什么样的人/内容、排除什么。例：作者本人在抱怨某类工具太贵或在找替代；排除新闻、教程、招聘、广告。",
    "langs": "只保留这些语言的推文。推荐按目标人群勾 1~3 个；留空=不限。",
    "min_score": "AI 给每条推文打 0-10 分，≥ 此分才进下一步。原则是默认保留：沾边就 ≥6，新闻/广告 3~5，看不懂 0~2。推荐 5；想更严 7。",
    "max_results": "每次运行最多拉多少条。推荐 15~30；官方 API 按条计费（约 $0.005/条），小号 Cookie 通道建议 ≤50 防风控。",
    "lookback": "首次运行（或重置游标后）往回抓多少小时内的推文；之后每次只抓上次之后的新内容。推荐 24；冷门词可 72~168。"
                "官方 API 最多只能搜最近 7 天（168 小时），填得再大也按 168 抓。",
    "min_views": "只要观看量 ≥ 此值的推文，0 = 不限。门槛在抓取端生效：一页不够会继续翻页，直到凑够「每次抓取条数」或扫到上限"
                 "（每次抓取条数 × 10，最多 500 条，且不超过当日剩余读额度）；低于门槛的当场丢掉、不入库，游标照常推进。"
                 "官方 API 按扫描到的条数计费。推荐：想找有热度的帖子 1000~5000；冷门领域填 0。",
}


def _hint(key: str):
    ui.label(HINTS[key]).classes("text-xs text-gray-400 -mt-2 mb-1")


def _save(rid, data: dict):
    with get_conn() as conn:
        if rid:
            conn.execute("UPDATE search_rules SET name=?, keyword_query=?, semantic_criteria=?, lang=?, min_llm_score=?, "
                         "max_results_per_run=?, lookback_hours=?, min_views=?, reply_mode=?, ai_brief=?, allow_polish=? WHERE id=?",
                         (data["name"], data["kq"], data["sc"], data["lang"], data["min_score"], data["max_results"],
                          data["lookback"], data["min_views"], data["reply_mode"], data["ai_brief"], data["polish"], rid))
        else:
            conn.execute("INSERT INTO search_rules(name, keyword_query, semantic_criteria, lang, min_llm_score, "
                         "max_results_per_run, lookback_hours, min_views, reply_mode, ai_brief, allow_polish) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         (data["name"], data["kq"], data["sc"], data["lang"], data["min_score"], data["max_results"],
                          data["lookback"], data["min_views"], data["reply_mode"], data["ai_brief"], data["polish"]))
        conn.commit()


def _rule_counts() -> dict[int, dict[str, int]]:
    with get_conn() as conn:
        rows = conn.execute("SELECT source_rule_id AS rid, process_status AS st, COUNT(*) AS c FROM target_tweets "
                            "WHERE source='search' GROUP BY 1, 2").fetchall()
    out: dict[int, dict[str, int]] = {}
    for r in rows:
        out.setdefault(r["rid"], {})[r["st"]] = r["c"]
    return out


def register(jobs) -> None:
    @ui.page("/rules")
    def rules_page():
        with shell("/rules"):
            with ui.row().classes("items-center justify-between w-full"):
                ui.label("搜索规则").classes("text-2xl font-bold")
                with ui.row().classes("gap-2"):
                    ui.button("AI 生成规则", icon="auto_awesome", on_click=lambda: _ai_generate(jobs, render)).props("outline color=purple")
                    ui.button("新建规则", icon="add", on_click=lambda: _edit(None, render)).props("color=primary")
                    ui.button("运行所有规则", icon="play_arrow",
                              on_click=lambda: run_job_with_progress(
                                  lambda progress: jobs.search.run_once(progress=progress), "搜索", render,
                                  result_link=("查看抓取记录", "/targets?source=search"))
                              ).props("outline").tooltip("把所有「启用」的规则各跑一次；只想跑某一条就点该规则卡片上的「运行此规则」")
            ui.label("两级漏斗：关键词查询粗筛（X 搜索语法，自动带上所选语言）→ 按语义条件给每条推文打 0-10 分 → "
                     "分数 ≥ 达标分的按规则的「回复方式」生成草稿、进审核队列。抓到的每一条（含未达标的、以及为什么）都在「抓取记录」页。"
                     ).classes("text-xs text-gray-400")
            llm_on = jobs.llm.configured
            if not llm_on:
                with ui.row().classes("items-center gap-1 text-xs text-orange-600"):
                    ui.icon("info")
                    ui.label("当前没配置 LLM：打分是关键词粗估；「AI 生成规则」「AI 按要求创作」不可用。请到")
                    ui.link("设置 → LLM", "/settings").classes("text-xs")
                    ui.label("配置网关。")
            with ui.expansion("当前的过滤规则是什么？（一条推文要过几关）", icon="rule").classes("w-full text-sm"):
                cd = config.get_int("cooldown_days", 7)
                ui.markdown(
                    "抓到的每条推文按顺序过以下几关，**任何一关没过都会写进「抓取记录」并注明原因**，不会悄悄丢掉：\n\n"
                    "1. **时间窗**：首次运行只看规则「首次回溯」小时数内的推文（官方 API 最多 7 天）；之后只抓上次游标之后的新内容。\n"
                    "2. **语言**：推文语言必须在规则勾选的语言内（留空 = 不限）。语言不明（und）的一律放行。\n"
                    "3. **预检**：转推 / 自己账号发的 / 作者在黑名单 / 这条已回复过（去重账本）"
                    f" / 同一作者 **{cd} 天**内互动过（设置 → 合规参数「作者冷却天数」）→ 跳过。前三关不花 LLM。\n"
                    "4. **相关性打分 ≥ 达标分**（每条规则可改）。打分原则是「默认保留」：\n"
                    + ("   - 已配置 LLM：按你写的语义条件打分——本人明确符合 8~10；沾边但拿不准 6~7；"
                       "新闻/教程/招聘/纯广告 3~5；无关或看不出意思 0~2。\n" if llm_on else
                       "   - 未配置 LLM（当前）：关键词已命中且有完整上下文 → 7；命中「新闻/招聘/募集/教程/リリース/hiring…」→ 3；"
                       "去掉链接、@、#、表情后不足 12 个字 → 2。\n")
                    + "5. **生成回复**（按规则的「回复方式」）：匹配素材库只要素材库里有启用的回复素材就一定给一条草稿（优先同语言、AI 择优，AI 拒绝或拿不准时按规则兜底）；AI 创作需要 LLM 和创作要求；"
                    "「只抓取」则等你手动处理。没生成成功的标为「达标但未生成回复」，可在抓取记录里手动「选素材」或「AI 撰写」。\n\n"
                    "过关的才生成回复草稿进「审核队列」，最后由你决定发不发。"
                ).classes("text-xs text-gray-600")

            body = ui.column().classes("w-full gap-2")

            async def delete(r):
                if await confirm(f"删除规则「{r['name']}」？", "已抓取的记录会保留。"):
                    _delete(r["id"]); ui.notify("已删除", type="positive"); render()

            def render():
                body.clear()
                with get_conn() as conn:
                    rows = conn.execute("SELECT * FROM search_rules ORDER BY id").fetchall()
                counts = _rule_counts()
                with body:
                    if not rows:
                        ui.label("暂无搜索规则。点「新建规则」手动填，或点「AI 生成规则」用大白话描述你想找谁。").classes("text-gray-400")
                        return
                    for r in rows:
                        c = counts.get(r["id"], {})
                        total = sum(c.values())
                        with ui.card().classes("w-full"):
                            with ui.row().classes("items-center gap-2 w-full"):
                                ui.label(r["name"]).classes("font-semibold")
                                ui.badge(langs_label(rule_langs(r))).classes("bg-slate-500")
                                ui.badge(f"达标分 ≥{r['min_llm_score']}").classes("bg-blue-600")
                                ui.badge(f"每次 {r['max_results_per_run']} 条").classes("bg-slate-400")
                                ui.badge(f"首次回溯 {r['lookback_hours']}h").classes("bg-slate-400")
                                if r["min_views"]:
                                    ui.badge(f"观看 ≥ {fmt_views(r['min_views'])}").classes("bg-amber-600")
                                ui.badge(REPLY_MODE_LABEL.get(r["reply_mode"], r["reply_mode"])).classes(
                                    "bg-purple-600" if r["reply_mode"] == "ai_write" else "bg-teal-600")
                                if not r["enabled"]:
                                    ui.badge("已停用").classes("bg-gray-400")
                                ui.label(f"上次运行 {fmt_time(r['last_run_at']) if r['last_run_at'] else '未运行'}"
                                         f" · 游标 {r['newest_id_cursor'] or '无（下次按首次回溯抓）'}").classes("text-xs text-gray-400")
                                ui.space()
                                sw = ui.switch("启用", value=bool(r["enabled"]))
                                sw.on("update:model-value", lambda e, rid=r["id"]: _toggle(rid, e.args))
                            ui.label("关键词：" + r["keyword_query"]).classes("text-xs font-mono text-gray-600")
                            ui.label("实际查询：" + effective_query(r)).classes("text-xs font-mono text-gray-400")
                            ui.label("语义：" + r["semantic_criteria"]).classes("text-sm")
                            if r["reply_mode"] == "ai_write":
                                ui.label("创作要求：" + (r["ai_brief"] or "（未填！AI 无法创作）")).classes(
                                    "text-xs " + ("text-gray-500" if r["ai_brief"] else "text-red-500"))
                            if total:
                                ui.label(f"累计抓取 {total} 条：进审核队列 {c.get('queued', 0)} · 达标但未生成回复 {c.get('no_match', 0)}"
                                         f" · 未达标/被过滤 {c.get('filtered', 0)} · 待匹配 {c.get('new', 0)} · 已过期 {c.get('expired', 0)}"
                                         ).classes("text-xs text-gray-500")
                            else:
                                ui.label("还没有抓取记录").classes("text-xs text-gray-400")
                            with ui.row().classes("gap-2"):
                                ui.button("查看结果", icon="travel_explore",
                                          on_click=lambda rid=r["id"]: ui.navigate.to(f"/targets?source=search&rule={rid}")
                                          ).props("flat dense color=primary")
                                ui.button("编辑", on_click=lambda rr=r: _edit(rr, render)).props("flat dense")
                                ui.button("运行此规则", icon="play_arrow",
                                          on_click=lambda rid=r["id"]: run_job_with_progress(
                                              lambda progress, rid=rid: jobs.search.run_once(rule_ids=[rid], progress=progress), "搜索", render,
                                              result_link=("查看这条规则的结果", f"/targets?source=search&rule={rid}"))
                                          ).props("flat dense").tooltip("只跑这一条规则（停用状态也能跑）；结果进抓取记录、推进游标，和正式运行一样")
                                ui.button("重置游标", on_click=lambda rid=r["id"]: (_reset_cursor(rid), ui.notify("已重置，下次搜索按「首次回溯」小时数重新抓", type="info"), render())).props("flat dense")
                                ui.button("删除", icon="delete", on_click=lambda rr=r: delete(rr)).props("flat dense color=negative")

            render()

    def _edit(r, refresh, preset: dict | None = None):
        """r=已有规则行；preset=AI 生成的预填值（新建时用）。"""
        p = preset or {}
        g = (lambda k, d: (r[k] if r is not None else p.get(k, d)))
        with ui.dialog() as dialog, ui.card().classes("w-[720px] max-w-[95vw] max-h-[92vh] overflow-auto"):
            ui.label("编辑规则" if r else "新建规则").classes("text-lg font-bold")
            name = ui.input("规则名", value=g("name", "")).classes("w-full").props("outlined")
            kq = ui.textarea("关键词（逗号隔开 = 命中任意一个即可）", value=g("keyword_query", "")).classes("w-full").props("outlined autogrow")
            _hint("keywords")
            sc = ui.textarea("语义筛选条件（大白话写给 AI 看）", value=g("semantic_criteria", "")).classes("w-full").props("outlined autogrow")
            _hint("semantic")
            cur_langs = rule_langs(r) if r else list(p.get("langs", ["ja"]))
            lang = ui.select(_LANG_OPTIONS, value=[x for x in cur_langs if x in _LANG_OPTIONS], multiple=True,
                             label="推文语言（可多选）").classes("w-full").props("outlined use-chips")
            _hint("langs")
            with ui.row().classes("w-full gap-3 no-wrap"):
                min_score = ui.number("达标分（0-10）", value=g("min_llm_score", 5), min=0, max=10, step=1).classes("flex-1").props("outlined")
                max_results = ui.number("每次抓取条数（10-100）", value=g("max_results_per_run", 15), min=10, max=100, step=1).classes("flex-1").props("outlined")
                lookback = ui.number("首次回溯（小时）", value=g("lookback_hours", 24), min=1, max=720, step=1).classes("flex-1").props("outlined")
            _hint("min_score"); _hint("max_results"); _hint("lookback")
            min_views = ui.number("观看量门槛（0 = 不限）", value=g("min_views", 0), min=0, step=100).classes("w-full").props("outlined")
            _hint("min_views")
            mode, brief, polish = reply_mode_fields(g("reply_mode", "material"), g("ai_brief", ""), g("allow_polish", 0), "抓到达标推文后")

            def do_save():
                if not (name.value or "").strip() or not (kq.value or "").strip() or not (sc.value or "").strip():
                    ui.notify("规则名 / 关键词 / 语义条件都不能为空", type="negative"); return
                problem = reply_mode_invalid(mode, brief)
                if problem:
                    ui.notify(problem, type="negative"); return
                data = dict(name=name.value.strip(), kq=kq.value.strip(), sc=sc.value.strip(),
                            lang=",".join(x for x in (lang.value or []) if x),
                            min_score=max(0, min(10, int(min_score.value or 0))),
                            max_results=max(10, min(100, int(max_results.value or 10))),
                            lookback=max(1, int(lookback.value or 24)),
                            min_views=max(0, int(min_views.value or 0)),
                            reply_mode=mode.value, ai_brief=(brief.value or "").strip(), polish=1 if polish.value else 0)
                try:
                    _save(r["id"] if r else None, data)
                except Exception as e:
                    ui.notify(f"保存失败：{e}（规则名可能重复）", type="negative"); return
                dialog.close(); refresh(); ui.notify("已保存", type="positive")

            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("取消", on_click=dialog.close).props("flat")
                ui.button("保存", on_click=do_save).props("color=primary")
        dialog.open()

    async def _ai_generate(jobs, refresh):
        if not jobs.llm.configured:
            ui.notify("「AI 生成规则」需要先到「设置 → LLM」配置网关", type="warning", multi_line=True); return
        with ui.dialog() as dlg, ui.card().classes("w-[640px] max-w-[95vw]"):
            ui.label("AI 生成搜索规则").classes("text-lg font-bold")
            desc = ui.textarea("用大白话描述你想找什么人 / 什么内容", value="").classes("w-full").props("outlined autogrow")
            ui.label("例：找在推特上抱怨某类软件订阅太贵、或者在问有没有更便宜替代品的日本独立开发者和小团队；"
                     "不要新闻和卖课的。AI 会给出关键词列表、语义条件和语言，你可以再改。").classes("text-xs text-gray-400")
            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("取消", on_click=lambda: dlg.submit(None)).props("flat")
                ui.button("生成", icon="auto_awesome", on_click=lambda: dlg.submit(desc.value or "")).props("color=primary")
        dlg.open()
        text = await dlg
        if not text or not text.strip():
            return
        ui.notify("AI 生成中…", type="info")
        try:
            obj = await run.io_bound(jobs.llm.generate_search_rule, text.strip())
        except Exception as e:
            ui.notify(f"生成失败：{e}", type="negative", multi_line=True, close_button=True); return
        preset = {
            "name": obj.get("name") or "AI 规则",
            "keyword_query": ", ".join(obj.get("keywords") or []),
            "semantic_criteria": obj.get("semantic_criteria") or "",
            "langs": obj.get("langs") or ["ja"],
        }
        ui.notify("已生成，请检查后保存", type="positive")
        _edit(None, refresh, preset=preset)


def _toggle(rid: int, enabled):
    with get_conn() as conn:
        conn.execute("UPDATE search_rules SET enabled=? WHERE id=?", (1 if enabled else 0, rid))
        conn.commit()


def _reset_cursor(rid: int):
    with get_conn() as conn:
        conn.execute("UPDATE search_rules SET newest_id_cursor=NULL WHERE id=?", (rid,))
        conn.commit()


def _delete(rid: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM search_rules WHERE id=?", (rid,))
        conn.commit()
