"""搜索规则（design-v1.1 §8.5）：关键词 + 语义条件 + 阈值，支持启停、试运行、重置游标。"""
from __future__ import annotations

from nicegui import run, ui

from ..core.monitor import get_primary_account
from ..db.database import get_conn
from .layout import confirm, fmt_time, run_job, shell


def _save(rid, name, kq, sc, lang, min_score, max_results):
    with get_conn() as conn:
        if rid:
            conn.execute("UPDATE search_rules SET name=?, keyword_query=?, semantic_criteria=?, lang=?, "
                         "min_llm_score=?, max_results_per_run=? WHERE id=?",
                         (name, kq, sc, lang, min_score, max_results, rid))
        else:
            conn.execute("INSERT INTO search_rules(name, keyword_query, semantic_criteria, lang, min_llm_score, max_results_per_run) "
                         "VALUES (?,?,?,?,?,?)", (name, kq, sc, lang, min_score, max_results))
        conn.commit()


def register(jobs) -> None:
    @ui.page("/rules")
    def rules_page():
        with shell("/rules"):
            with ui.row().classes("items-center justify-between w-full"):
                ui.label("搜索规则").classes("text-2xl font-bold")
                with ui.row():
                    ui.button("新建规则", icon="add", on_click=lambda: _edit(None, render)).props("color=primary")
                    ui.button("运行一次搜索", icon="play_arrow", on_click=lambda: run_job(jobs.search.run_once, "搜索", render)).props("outline")
            ui.label("两级漏斗：关键词查询粗筛（X 搜索语法）→ LLM 按语义条件打分 → 达标的匹配素材进审核队列。"
                     "抓到的推文（含未达标的）都在「抓取记录」页。").classes("text-xs text-gray-400")

            body = ui.column().classes("w-full gap-2")

            async def delete(r):
                if await confirm(f"删除规则「{r['name']}」？", "已抓取的记录会保留。"):
                    _delete(r["id"]); ui.notify("已删除", type="positive"); render()

            def render():
                body.clear()
                with get_conn() as conn:
                    rows = conn.execute("SELECT * FROM search_rules ORDER BY id").fetchall()
                with body:
                    if not rows:
                        ui.label("暂无搜索规则").classes("text-gray-400")
                        return
                    for r in rows:
                        with ui.card().classes("w-full"):
                            with ui.row().classes("items-center gap-2 w-full"):
                                ui.label(r["name"]).classes("font-semibold")
                                ui.badge(r["lang"]).classes("bg-slate-500")
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
                            ui.label("语义：" + r["semantic_criteria"]).classes("text-sm")
                            with ui.row().classes("gap-2"):
                                ui.button("编辑", on_click=lambda rr=r: _edit(rr, render)).props("flat dense")
                                ui.button("试运行", icon="science", on_click=lambda rr=r: _dry_run(jobs, rr)).props("flat dense")
                                ui.button("重置游标", on_click=lambda rid=r["id"]: (_reset_cursor(rid), ui.notify("已重置，下次搜索会重新抓最近的推文", type="info"), render())).props("flat dense")
                                ui.button("删除", icon="delete", on_click=lambda rr=r: delete(rr)).props("flat dense color=negative")

            render()

    async def _dry_run(jobs, rule):
        account = get_primary_account()
        if account is None:
            ui.notify("没有状态为「启用」的账号，无法搜索", type="negative"); return
        with ui.dialog() as dialog, ui.card().classes("min-w-[700px] max-w-[90vw]"):
            ui.label(f"试运行：{rule['name']}").classes("text-lg font-bold")
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
                    ui.label("没抓到新推文（游标之后没有新内容，可先「重置游标」）").classes("text-gray-500")
                    return
                rows = [{"作者": "@" + c.tweet.author_handle, "文本": c.tweet.text[:80], "分数": c.score, "理由": c.reason,
                         "达标": "✓" if c.score >= rule["min_llm_score"] else ""} for c in scored]
                ui.table(columns=[{"name": k, "label": k, "field": k, "align": "left"} for k in ("作者", "文本", "分数", "理由", "达标")],
                         rows=rows).classes("w-full")
                ui.label(f"共 {len(rows)} 条，达标 {sum(1 for r in rows if r['达标'])} 条").classes("text-xs text-gray-400")
        except Exception as e:
            container.clear()
            with container:
                ui.label(f"试运行出错：{e}").classes("text-red-500")

    def _edit(r, refresh):
        with ui.dialog() as dialog, ui.card().classes("min-w-[520px]"):
            ui.label("编辑规则" if r else "新建规则").classes("text-lg font-bold")
            name = ui.input("规则名", value=r["name"] if r else "").classes("w-full").props("outlined")
            kq = ui.textarea("关键词查询（X 搜索语法）", value=r["keyword_query"] if r else "").classes("w-full").props("outlined")
            ui.label("例：(API 料金 OR API コスト) (AI OR LLM) -is:retweet lang:ja").classes("text-xs text-gray-400")
            sc = ui.textarea("语义筛选条件（自然语言，给 LLM 看）", value=r["semantic_criteria"] if r else "").classes("w-full").props("outlined")
            lang = ui.select({"ja": "日语", "en": "英语", "zh": "中文"}, value=r["lang"] if r else "ja", label="语言").props("outlined")
            min_score = ui.number("LLM 达标分（0-10）", value=r["min_llm_score"] if r else 7, min=0, max=10).props("outlined")
            max_results = ui.number("每次抓取数（10-100）", value=r["max_results_per_run"] if r else 15, min=10, max=100).props("outlined")

            def do_save():
                if not name.value.strip() or not kq.value.strip() or not sc.value.strip():
                    ui.notify("规则名/关键词/语义条件均不能为空", type="negative"); return
                try:
                    _save(r["id"] if r else None, name.value.strip(), kq.value.strip(), sc.value.strip(),
                          lang.value, int(min_score.value or 0), int(max_results.value or 10))
                except Exception as e:
                    ui.notify(f"保存失败：{e}", type="negative"); return
                dialog.close(); refresh(); ui.notify("已保存", type="positive")

            with ui.row():
                ui.button("保存", on_click=do_save).props("color=primary")
                ui.button("取消", on_click=dialog.close).props("flat")
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
