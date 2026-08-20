"""公共页面外壳（design-v1.1 §8.0）：深色顶栏 + 醒目导航 + 内容插槽。"""
from __future__ import annotations

from contextlib import contextmanager

from nicegui import ui

from ..db.database import get_conn

# (路径, 名称, material 图标)
NAV = [
    ("/", "仪表盘", "dashboard"),
    ("/queue", "审核队列", "rate_review"),
    ("/materials", "素材库", "inventory_2"),
    ("/watched", "监控推主", "visibility"),
    ("/rules", "搜索规则", "manage_search"),
    ("/schedule", "定时计划", "schedule"),
    ("/settings", "设置", "settings"),
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
    dry = config.get_bool("dry_run", True)
    with ui.header().classes("items-center justify-between bg-slate-900 text-white px-4 py-2 shadow-lg gap-3"):
        # 左：品牌 + 运行模式徽标
        with ui.row().classes("items-center gap-2 shrink-0"):
            ui.icon("smart_toy").classes("text-2xl text-sky-400")
            ui.label("x-operator").classes("text-lg font-bold")
            mode = "Mock 演示" if dry else "真实发送"
            ui.badge(mode).classes(("bg-amber-500" if dry else "bg-red-600") + " text-white font-semibold")

        # 中：醒目导航区——成块底色 + 图标 + hover/active 高亮（密集操作区，视觉强化）
        pc = _pending_count()
        with ui.row().classes("items-center gap-1 bg-slate-800/70 rounded-xl p-1 flex-wrap"):
            for path, name, icon in NAV:
                is_active = path == active
                cls = ("flex items-center gap-1 px-3 py-1.5 rounded-lg no-underline "
                       "text-sm font-medium transition-colors ")
                cls += ("bg-sky-600 text-white shadow"
                        if is_active else
                        "text-slate-200 hover:bg-slate-700 hover:text-white")
                with ui.link(target=path).classes(cls):
                    ui.icon(icon).classes("text-lg")
                    ui.label(name)
                    if path == "/queue" and pc:
                        ui.badge(str(pc)).classes("bg-red-600 text-white ml-1")

        # 右：账号告警
        ac = _alert_count()
        if ac:
            ui.badge(f"⚠ {ac} 账号凭据失效").classes("bg-red-600 text-white shrink-0")
    container = ui.column().classes("max-w-5xl mx-auto p-4 w-full")
    with container:
        yield container
