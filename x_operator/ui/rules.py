"""搜索规则（design-v1.1 §8.5）：关键词 + 语义条件 + 达标分，支持多语言、启停、试运行、重置游标。

搜索结果不在本页显示——每条抓到的推文（含未达标的及原因）都在「抓取记录」页，本页每条规则
都有「查看结果」直达按钮，运行完也会弹出带跳转按钮的结果框。
"""
from __future__ import annotations

from nicegui import run, ui

from .. import config
from ..core.monitor import get_primary_account
from ..core.search import LANG_LABEL, effective_query, langs_label, rule_langs
from ..db.database import get_conn
from .layout import confirm, fmt_time, run_job, shell

_LANG_OPTIONS = {k: v for k, v in LANG_LABEL.items()}


def _save(rid, name, kq, sc, langs: list[str], min_score, max_results):
    lang_str = ",".join(langs)
    with get_conn() as conn:
        if rid:
            conn.execute("UPDATE search_rules SET name=?, keyword_query=?, semantic_criteria=?, lang=?, "
                         "min_llm_score=?, max_results_per_run=? WHERE id=?",
                         (name, kq, sc, lang_str, min_score, max_results, rid))
        else:
            conn.execute("INSERT INTO search_rules(name, keyword_query, semantic_criteria, lang, min_llm_score, max_results_per_run) "
                         "VALUES (?,?,?,?,?,?)", (name, kq, sc, lang_str, min_score, max_results))
        conn.commit()


def _rule_counts() -> dict[int, dict[str, int]]:
    """每条规则的抓取结果分布 {rule_id: {status: n}}。"""
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
                with ui.row():
                    ui.button("新建规则", icon="add", on_click=lambda: _edit(None, render)).props("color=primary")
                    ui.button("运行一次搜索", icon="play_arrow",
                              on_click=lambda: run_job(jobs.search.run_once, "搜索", render,
                                                       result_link=("查看抓取记录", "/targets?source=search"))).props("outline")
            ui.label("两级漏斗：关键词查询粗筛（X 搜索语法，自动带上所选语言）→ 按语义条件给每条推文打 0-10 分 → "
                     "分数 ≥ 达标分的去匹配素材、进审核队列。抓到的每一条（含未达标的、以及为什么）都在「抓取记录」页。"
                     ).classes("text-xs text-gray-400")
            llm_on = bool(config.get("llm_base_url") and config.get("llm_api_key"))
            if not llm_on:
                with ui.row().classes("items-center gap-1 text-xs text-orange-600"):
                    ui.icon("info")
                    ui.label("当前没配置 LLM：打分是关键词粗估（默认 7 分保留，新闻/广告 3，无上下文 2）。想按语义条件精挑请到")
                    ui.link("设置 → LLM", "/settings").classes("text-xs")
                    ui.label("配置网关。")
            with ui.expansion("当前的过滤规则是什么？（一条推文要过几关）", icon="rule").classes("w-full text-sm"):
                age_h = config.get_int("tweet_max_age_hours", 168)
                cd = config.get_int("cooldown_days", 7)
                ui.markdown(
                    "抓到的每条推文按顺序过以下几关，**任何一关没过都会写进「抓取记录」并注明原因**，不会悄悄丢掉：\n\n"
                    "1. **语言**：推文语言必须在规则勾选的语言内（留空 = 不限）。语言不明（und）的一律放行。\n"
                    "2. **相关性打分 ≥ 达标分**（本页每条规则可改）。打分原则是「默认保留」：\n"
                    + ("   - 已配置 LLM：按你写的语义条件打分——本人明确符合 8~10；沾边但拿不准 6~7；"
                       "新闻/教程/招聘/纯广告 3~5；无关或看不出意思 0~2。\n" if llm_on else
                       "   - 未配置 LLM（当前）：关键词已命中且有完整上下文 → 7；命中「新闻/招聘/募集/教程/リリース/hiring…」→ 3；"
                       "去掉链接、@、#、表情后不足 12 个字 → 2。\n")
                    + "3. **预检**（设置 → 合规参数 / 黑名单 可调）：\n"
                    "   - 转推 → 跳过\n"
                    "   - 自己账号发的 → 跳过\n"
                    f"   - 发推时间超过 **{age_h} 小时**（推文最大年龄）→ 跳过\n"
                    "   - 作者在黑名单 → 跳过\n"
                    "   - 这条推文已经回复过（去重账本）→ 跳过\n"
                    f"   - 同一作者 **{cd} 天**内互动过（作者冷却天数）→ 跳过\n"
                    "4. **匹配素材**：素材库里要有同语言、启用中的「回复」素材；没有则标为「达标但没配到素材」，补素材后可「重新匹配」。\n\n"
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
                        ui.label("暂无搜索规则，点右上「新建规则」。").classes("text-gray-400")
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
                                if not r["enabled"]:
                                    ui.badge("已停用").classes("bg-gray-400")
                                ui.label(f"上次运行 {fmt_time(r['last_run_at']) if r['last_run_at'] else '未运行'}"
                                         f" · 游标 {r['newest_id_cursor'] or '无'}").classes("text-xs text-gray-400")
                                ui.space()
                                sw = ui.switch("启用", value=bool(r["enabled"]))
                                sw.on("update:model-value", lambda e, rid=r["id"]: _toggle(rid, e.args))
                            ui.label("关键词：" + r["keyword_query"]).classes("text-xs font-mono text-gray-600")
                            eq = effective_query(r)
                            if eq != r["keyword_query"].strip():
                                ui.label("实际查询：" + eq).classes("text-xs font-mono text-gray-400")
                            ui.label("语义：" + r["semantic_criteria"]).classes("text-sm")
                            if total:
                                ui.label(f"累计抓取 {total} 条：进审核队列 {c.get('queued', 0)} · 达标但没配到素材 {c.get('no_match', 0)}"
                                         f" · 未达标/被过滤 {c.get('filtered', 0)} · 待匹配 {c.get('new', 0)} · 已过期 {c.get('expired', 0)}"
                                         ).classes("text-xs text-gray-500")
                            else:
                                ui.label("还没有抓取记录").classes("text-xs text-gray-400")
                            with ui.row().classes("gap-2"):
                                ui.button("查看结果", icon="travel_explore",
                                          on_click=lambda rid=r["id"]: ui.navigate.to(f"/targets?source=search&rule={rid}")
                                          ).props("flat dense color=primary")
                                ui.button("编辑", on_click=lambda rr=r: _edit(rr, render)).props("flat dense")
                                ui.button("试运行", icon="science", on_click=lambda rr=r: _preview(jobs, rr)).props("flat dense")
                                ui.button("重置游标", on_click=lambda rid=r["id"]: (_reset_cursor(rid), ui.notify("已重置，下次搜索会重新抓最近的推文", type="info"), render())).props("flat dense")
                                ui.button("删除", icon="delete", on_click=lambda rr=r: delete(rr)).props("flat dense color=negative")

            render()

    async def _preview(jobs, rule):
        account = get_primary_account()
        if account is None:
            ui.notify("没有状态为「启用」的账号，无法搜索", type="negative"); return
        with ui.dialog() as dialog, ui.card().classes("min-w-[760px] max-w-[95vw]"):
            ui.label(f"试运行：{rule['name']}").classes("text-lg font-bold")
            ui.label("实际查询：" + effective_query(rule)).classes("text-xs font-mono text-gray-500")
            ui.label("（会消耗读额度；仅打分预览，不写库、不推进游标、不进队列）").classes("text-xs text-gray-400")
            container = ui.column().classes("w-full")
            with container:
                ui.spinner()
                ui.label("正在抓取并打分…").classes("text-xs text-gray-400")
            ui.button("关闭", on_click=dialog.close).props("flat")
        dialog.open()
        try:
            scored = await run.io_bound(jobs.search.run_rule, rule, account, True)
            container.clear()
            with container:
                if not scored:
                    ui.label("没抓到新推文（游标之后没有新内容，可先「重置游标」；或者关键词太窄）").classes("text-gray-500")
                    return
                rows = [{"作者": "@" + c.tweet.author_handle, "语言": c.tweet.lang or "?", "文本": c.tweet.text[:100],
                         "分数": c.score, "理由": c.reason,
                         "达标": "✓" if c.score >= rule["min_llm_score"] else "✗"} for c in scored]
                ui.table(columns=[{"name": k, "label": k, "field": k, "align": "left"} for k in ("作者", "语言", "文本", "分数", "理由", "达标")],
                         rows=rows).classes("w-full").props("wrap-cells dense")
                ui.label(f"共 {len(rows)} 条，达标（≥{rule['min_llm_score']}） {sum(1 for r in rows if r['达标'] == '✓')} 条").classes("text-xs text-gray-400")
        except Exception as e:
            container.clear()
            with container:
                ui.label(f"试运行出错：{e}").classes("text-red-500 whitespace-pre-wrap")

    def _edit(r, refresh):
        with ui.dialog() as dialog, ui.card().classes("w-[640px] max-w-[95vw]"):
            ui.label("编辑规则" if r else "新建规则").classes("text-lg font-bold")
            name = ui.input("规则名", value=r["name"] if r else "").classes("w-full").props("outlined")
            kq = ui.textarea("关键词（逗号隔开 = 命中任意一个即可）", value=r["keyword_query"] if r else "").classes("w-full").props("outlined autogrow")
            ui.label("最简单的写法：adult, nsfw, AI美女, AI短剧 → 含其中任意一个词的推文都会被抓（中文词会自动整词匹配）。"
                     "高级写法用 X 语法：空格=同时包含，OR=或，-词=排除，\"短语\"=整句，例：(API 料金 OR API コスト) (AI OR LLM)。"
                     "语言不用写，下面勾选；转推默认排除。保存后卡片上会显示实际发给 X 的查询。").classes("text-xs text-gray-400")
            sc = ui.textarea("语义筛选条件（用大白话写给 AI 看：要什么样的人、排除什么）",
                             value=r["semantic_criteria"] if r else "").classes("w-full").props("outlined autogrow")
            ui.label("例：作者本人正在为 AI 的 API 费用发愁或在找更便宜的替代方案；排除新闻、教程、招聘、广告。").classes("text-xs text-gray-400")
            cur_langs = rule_langs(r) if r else ["ja"]
            lang = ui.select(_LANG_OPTIONS, value=cur_langs, multiple=True, label="推文语言（可多选，留空=不限）") \
                .classes("w-full").props("outlined use-chips")
            with ui.row().classes("w-full gap-3 no-wrap"):
                min_score = ui.number("达标分（0-10）", value=r["min_llm_score"] if r else 5, min=0, max=10, step=1) \
                    .classes("flex-1").props("outlined")
                max_results = ui.number("每次抓取条数（10-100）", value=r["max_results_per_run"] if r else 15, min=10, max=100, step=1) \
                    .classes("flex-1").props("outlined")
            ui.label("达标分：AI 给每条推文打 0-10 分，≥ 达标分才去匹配素材。打分是「默认保留」：能看懂且沾边 ≥6，"
                     "新闻/招聘/广告 3~5，无关或看不出意思 0~2。推荐 5（只剔除明显没用的）；想更严就 7。"
                     "每次抓取条数：每次运行最多拉几条，官方 API 按条计费。").classes("text-xs text-gray-400")

            def do_save():
                if not (name.value or "").strip() or not (kq.value or "").strip() or not (sc.value or "").strip():
                    ui.notify("规则名 / 关键词 / 语义条件都不能为空", type="negative"); return
                langs = [x for x in (lang.value or []) if x]
                try:
                    _save(r["id"] if r else None, name.value.strip(), kq.value.strip(), sc.value.strip(),
                          langs, int(min_score.value or 0), int(max_results.value or 10))
                except Exception as e:
                    ui.notify(f"保存失败：{e}（规则名可能重复）", type="negative"); return
                dialog.close(); refresh(); ui.notify("已保存", type="positive")

            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("取消", on_click=dialog.close).props("flat")
                ui.button("保存", on_click=do_save).props("color=primary")
        dialog.open()


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
