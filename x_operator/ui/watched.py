"""监控推主（design-v1.1 §8.4）：添加/删除/启停被监控的推主。"""
from __future__ import annotations

from nicegui import ui

from ..adapters import factory
from ..adapters.base import XClientError
from ..core.monitor import get_primary_account
from ..db.database import get_conn, utcnow_iso
from .layout import shell


def _add(handle: str) -> str:
    handle = handle.lstrip("@").strip()
    if not handle:
        return "请输入 @handle"
    account = get_primary_account()
    if account is None:
        return "没有可用的官方账号，无法解析用户"
    try:
        client = factory.get_client(account)
        user = client.get_user_by_handle(handle)
    except XClientError as e:
        return f"解析失败：{e}"
    with get_conn() as conn:
        try:
            conn.execute("INSERT INTO watched_users(handle, x_user_id, created_at) VALUES (?,?,?)",
                         (user.handle, user.user_id, utcnow_iso()))
            conn.commit()
        except Exception:
            return "该推主已在监控列表中"
    return ""


def register(jobs) -> None:
    @ui.page("/watched")
    def watched_page():
        with shell("/watched"):
            ui.label("监控推主").classes("text-2xl font-bold")
            with ui.row().classes("items-center gap-2"):
                inp = ui.input("@handle").props("outlined dense")

                def add():
                    err = _add(inp.value)
                    if err:
                        ui.notify(err, type="negative")
                    else:
                        ui.notify("已添加", type="positive"); inp.value = ""; render()
                ui.button("添加", on_click=add)
                ui.button("运行一次监控", on_click=lambda: _run(jobs, render)).props("outline")

            body = ui.column().classes("w-full gap-2")

            def render():
                body.clear()
                with get_conn() as conn:
                    rows = conn.execute("SELECT * FROM watched_users ORDER BY id").fetchall()
                with body:
                    if not rows:
                        ui.label("暂无监控推主").classes("text-gray-400")
                        return
                    for u in rows:
                        with ui.card().classes("w-full"):
                            with ui.row().classes("items-center justify-between w-full"):
                                with ui.column().classes("gap-0"):
                                    ui.label(f"@{u['handle']}").classes("font-semibold")
                                    ui.label(f"命中 {u['hit_count']} 次 · 游标 {u['last_seen_tweet_id'] or '未设置'}"
                                             + (f" · {u['note']}" if u['note'] else "")).classes("text-xs text-gray-400")
                                with ui.row().classes("items-center gap-2"):
                                    sw = ui.switch("启用", value=bool(u["enabled"]))
                                    sw.on("update:model-value", lambda e, uid=u["id"]: _toggle(uid, e.args))
                                    ui.button("删除", on_click=lambda uid=u["id"]: (_delete(uid), render())).props("flat color=negative")

            render()

    def _run(jobs, refresh):
        try:
            stats = jobs.monitor.run_once()
            ui.notify(stats.as_msg(), type="positive")
        except Exception as e:
            ui.notify(f"监控出错：{e}", type="negative")
        refresh()


def _toggle(uid: int, enabled):
    with get_conn() as conn:
        conn.execute("UPDATE watched_users SET enabled=? WHERE id=?", (1 if enabled else 0, uid))
        conn.commit()


def _delete(uid: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM watched_users WHERE id=?", (uid,))
        conn.commit()
