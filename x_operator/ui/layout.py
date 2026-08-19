"""公共页面外壳（design-v1.1 §8.0）：深色顶栏 + 导航 + 内容插槽。"""
from __future__ import annotations

from contextlib import contextmanager

from nicegui import ui

from ..db.database import get_conn

NAV = [
    ("/", "仪表盘"),
    ("/queue", "审核队列"),
    ("/materials", "素材库"),
    ("/watched", "监控推主"),
    ("/rules", "搜索规则"),
    ("/schedule", "定时计划"),
    ("/settings", "设置"),
]


def _pending_count() -> int:
    with get_conn() as conn:
        row = conn.execute("SELECT COUNT(*) AS c FROM review_queue WHERE status='pending'").fetchone()
    return row["c"]


def _alert_count() -> int:
    with get_conn() as conn:
        row = conn.execute("SELECT COUNT(*) AS c FROM accounts WHERE status='auth_error'").fetchone()
    return row["c"]


@contextmanager
def shell(active: str):
    from .. import config
    with ui.header().classes("items-center justify-between bg-slate-900 text-white px-4 py-2"):
        with ui.row().classes("items-center gap-2"):
            ui.label("x-operator").classes("text-lg font-bold")
            mode = "Mock 演示" if config.get_bool("dry_run", True) else "真实发送"
            ui.badge(mode).classes("bg-amber-500" if config.get_bool("dry_run", True) else "bg-red-600")
        with ui.row().classes("items-center gap-1"):
            for path, name in NAV:
                classes = "text-white px-2 py-1 rounded no-underline"
                if path == active:
                    classes += " bg-slate-700 font-semibold"
                if path == "/queue":
                    pc = _pending_count()
                    label = f"{name}" + (f" ({pc})" if pc else "")
                    ui.link(label, path).classes(classes)
                else:
                    ui.link(name, path).classes(classes)
            ac = _alert_count()
            if ac:
                ui.badge(f"⚠ {ac} 账号凭据失效").classes("bg-red-600")
    container = ui.column().classes("max-w-5xl mx-auto p-4 w-full")
    with container:
        yield container
