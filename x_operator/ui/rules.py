"""搜索规则（design-v1.1 §8.5）：关键词 + 语义条件 + 阈值，支持试运行。"""
from __future__ import annotations

from nicegui import ui

from ..core.monitor import get_primary_account
from ..db.database import get_conn, utcnow_iso
from .layout import shell


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
                    ui.button("新建规则", on_click=lambda: _edit(None, render))
                    ui.button("运行一次搜索", on_click=lambda: _run(jobs, render)).props("outline")

            body = ui.column().classes("w-full gap-2")

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
                            with ui.row().classes("items-center gap-2"):
                                ui.label(r["name"]).classes("font-semibold")
                                ui.badge(r["lang"]).classes("bg-slate-500")
                                ui.badge(f"阈值 {r['min_llm_score']}").classes("bg-blue-600")
                                ui.label(f"上次运行 {r['last_run_at'] or '未运行'}").classes("text-xs text-gray-400")
                            ui.label("关键词：" + r["keyword_query"]).classes("text-xs font-mono text-gray-600")
                            ui.label("语义：" + r["semantic_criteria"]).classes("text-sm")
                            with ui.row().classes("gap-2"):
                                ui.button("编辑", on_click=lambda rr=r: _edit(rr, render)).props("flat")
                                ui.button("试运行", on_click=lambda rr=r: _dry_run(jobs, rr)).props("flat")
                                ui.button("删除", on_click=lambda rr=r: (_delete(rr["id"]), render())).props("flat color=negative")

            render()

    def _run(jobs, refresh):
        try:
            stats = jobs.search.run_once()
            ui.notify(stats.as_msg(), type="positive")
        except Exception as e:
            ui.notify(f"搜索出错：{e}", type="negative")
        refresh()

    def _dry_run(jobs, rule):
        account = get_primary_account()
        if account is None:
            ui.notify("没有可用官方账号", type="negative"); return
        with ui.dialog() as dialog, ui.card().classes("min-w-[600px]"):
            ui.label(f"试运行：{rule['name']}").classes("text-lg font-bold")
            ui.label("（会消耗读额度；仅打分预览，不写库、不进队列）").classes("text-xs text-gray-400")
            container = ui.column().classes("w-full")
            with container:
                ui.spinner()
            ui.button("关闭", on_click=dialog.close).props("flat")
        dialog.open()
        try:
            scored = jobs.search.run_rule(rule, account, dry_run=True)
            container.clear()
            with container:
                rows = [{"文本": c.tweet.text[:60], "分数": c.score, "理由": c.reason,
                         "达标": "✓" if c.score >= rule["min_llm_score"] else ""} for c in scored]
                ui.table(columns=[{"name": k, "label": k, "field": k} for k in ("文本", "分数", "理由", "达标")],
                         rows=rows).classes("w-full")
        except Exception as e:
            container.clear()
            with container:
                ui.label(f"试运行出错：{e}").classes("text-red-500")

    def _edit(r, refresh):
        with ui.dialog() as dialog, ui.card().classes("min-w-[520px]"):
            ui.label("编辑规则" if r else "新建规则").classes("text-lg font-bold")
            name = ui.input("规则名", value=r["name"] if r else "").classes("w-full").props("outlined")
            kq = ui.textarea("关键词查询（X 语法）", value=r["keyword_query"] if r else "").classes("w-full").props("outlined")
            sc = ui.textarea("语义筛选条件（自然语言）", value=r["semantic_criteria"] if r else "").classes("w-full").props("outlined")
            lang = ui.select({"ja": "日语", "en": "英语", "zh": "中文"}, value=r["lang"] if r else "ja").props("outlined")
            min_score = ui.number("LLM 达标分（0-10）", value=r["min_llm_score"] if r else 7, min=0, max=10).props("outlined")
            max_results = ui.number("每次抓取数（10-100）", value=r["max_results_per_run"] if r else 15, min=10, max=100).props("outlined")

            def do_save():
                if not name.value.strip() or not kq.value.strip() or not sc.value.strip():
                    ui.notify("规则名/关键词/语义条件均不能为空", type="negative"); return
                _save(r["id"] if r else None, name.value.strip(), kq.value.strip(), sc.value.strip(),
                      lang.value, int(min_score.value), int(max_results.value))
                dialog.close(); refresh(); ui.notify("已保存", type="positive")

            with ui.row():
                ui.button("保存", on_click=do_save).props("color=primary")
                ui.button("取消", on_click=dialog.close).props("flat")
        dialog.open()


def _delete(rid: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM search_rules WHERE id=?", (rid,))
        conn.commit()
