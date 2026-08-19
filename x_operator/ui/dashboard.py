"""仪表盘（design-v1.1 §8.1）：概况卡 + 读额度 + 最近异常 + 手动运行入口。"""
from __future__ import annotations

from nicegui import ui

from .. import config
from ..db.database import get_conn
from .layout import shell


def _stats() -> dict:
    with get_conn() as conn:
        sent_today = conn.execute(
            "SELECT COUNT(*) AS c FROM interactions WHERE sent_at>=strftime('%Y-%m-%dT00:00:00Z','now')"
        ).fetchone()["c"]
        pending = conn.execute("SELECT COUNT(*) AS c FROM review_queue WHERE status='pending'").fetchone()["c"]
        no_match = conn.execute("SELECT COUNT(*) AS c FROM target_tweets WHERE process_status='no_match'").fetchone()["c"]
        total_targets = conn.execute("SELECT COUNT(*) AS c FROM target_tweets").fetchone()["c"]
        fails = conn.execute(
            "SELECT COUNT(*) AS c FROM action_log WHERE success=0 AND created_at>=strftime('%Y-%m-%dT%H:%M:%SZ','now','-1 day')"
        ).fetchone()["c"]
        reads_today = conn.execute(
            "SELECT COALESCE(SUM(reads_consumed),0) AS c FROM action_log WHERE created_at>=strftime('%Y-%m-%dT00:00:00Z','now')"
        ).fetchone()["c"]
        recent_fail = conn.execute(
            "SELECT created_at, endpoint, error FROM action_log WHERE success=0 ORDER BY created_at DESC LIMIT 10"
        ).fetchall()
    return dict(sent_today=sent_today, pending=pending, no_match=no_match,
                total_targets=total_targets, fails=fails, reads_today=reads_today,
                recent_fail=recent_fail)


def register(jobs) -> None:
    @ui.page("/")
    def dashboard_page():
        with shell("/"):
            ui.label("仪表盘").classes("text-2xl font-bold")

            body = ui.column().classes("w-full gap-4")

            def render():
                body.clear()
                s = _stats()
                with body:
                    with ui.row().classes("gap-4 w-full"):
                        _card("今日发送", str(s["sent_today"]), "已发出的推文/回复")
                        _card("队列积压", str(s["pending"]), "待人工审核", warn=s["pending"] > 0)
                        rate = f'{(s["no_match"] / s["total_targets"] * 100):.0f}%' if s["total_targets"] else "—"
                        _card("no_match 率", rate, "抓取后未匹配比例")
                        _card("24h 异常", str(s["fails"]), "失败的 API/LLM 调用", warn=s["fails"] > 0)

                    with ui.card().classes("w-full"):
                        daily_budget = config.get_int("daily_read_budget", 330)
                        ui.label("读额度").classes("font-semibold")
                        pct = min(1.0, s["reads_today"] / daily_budget) if daily_budget else 0
                        ui.linear_progress(pct, show_value=False).classes("w-full")
                        ui.label(f'今日 {s["reads_today"]}/{daily_budget} 次读取 · 计费模式 {config.get("billing_mode")}')

                    with ui.card().classes("w-full"):
                        ui.label("手动运行（测试期用；自动轮询可在设置页打开）").classes("font-semibold")
                        with ui.row():
                            ui.button("运行监控轮询", on_click=lambda: _run(jobs.monitor.run_once, "监控", render))
                            ui.button("运行语义搜索", on_click=lambda: _run(jobs.search.run_once, "搜索", render))
                            ui.button("生成到点定时推文", on_click=lambda: _run_sched(jobs, render))
                            ui.button("触发发送分发", on_click=lambda: _run_dispatch(jobs, render))

                    with ui.card().classes("w-full"):
                        ui.label("最近异常").classes("font-semibold")
                        if not s["recent_fail"]:
                            ui.label("暂无异常 ✨")
                        else:
                            rows = [{"时间": r["created_at"], "来源": r["endpoint"], "说明": r["error"] or ""}
                                    for r in s["recent_fail"]]
                            ui.table(columns=[{"name": k, "label": k, "field": k} for k in ("时间", "来源", "说明")],
                                     rows=rows).classes("w-full")

            render()
            ui.timer(30.0, render)


def _card(title: str, value: str, sub: str, warn: bool = False):
    with ui.card().classes("min-w-40"):
        ui.label(title).classes("text-sm text-gray-500")
        ui.label(value).classes("text-3xl font-bold " + ("text-red-500" if warn else ""))
        ui.label(sub).classes("text-xs text-gray-400")


def _run(fn, name: str, refresh) -> None:
    try:
        stats = fn()
        ui.notify(stats.as_msg() if hasattr(stats, "as_msg") else f"{name}完成", type="positive")
    except Exception as e:
        ui.notify(f"{name}出错：{e}", type="negative")
    refresh()


def _run_sched(jobs, refresh) -> None:
    try:
        n = jobs.run_scheduled_posts()
        ui.notify(f"生成 {n} 条定时推文到审核队列", type="positive")
    except Exception as e:
        ui.notify(f"出错：{e}", type="negative")
    refresh()


def _run_dispatch(jobs, refresh) -> None:
    try:
        n = jobs.dispatcher.tick()
        ui.notify(f"本轮发送 {n} 条（Mock）", type="positive")
    except Exception as e:
        ui.notify(f"出错：{e}", type="negative")
    refresh()
