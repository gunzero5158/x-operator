"""定时计划（design-v1.1 §8.6）：定时发帖计划的增删改。"""
from __future__ import annotations

from datetime import datetime, timezone

from nicegui import ui

from ..core.schedule_calc import compute_next_run
from ..db.database import get_conn, utcnow_iso
from .layout import shell


def register(jobs) -> None:
    @ui.page("/schedule")
    def schedule_page():
        with shell("/schedule"):
            with ui.row().classes("items-center justify-between w-full"):
                ui.label("定时计划").classes("text-2xl font-bold")
                ui.button("新建计划", on_click=lambda: _edit(None, render))

            body = ui.column().classes("w-full gap-2")

            def render():
                body.clear()
                with get_conn() as conn:
                    rows = conn.execute(
                        "SELECT sp.*, a.handle AS acc_handle, m.text AS mat_text FROM scheduled_posts sp "
                        "JOIN accounts a ON a.id=sp.account_id JOIN materials m ON m.id=sp.material_id "
                        "ORDER BY sp.id").fetchall()
                with body:
                    if not rows:
                        ui.label("暂无定时计划").classes("text-gray-400")
                        return
                    for sp in rows:
                        with ui.card().classes("w-full"):
                            with ui.row().classes("items-center gap-2"):
                                ui.badge(f"@{sp['acc_handle']}").classes("bg-slate-600")
                                ui.badge(f"{sp['schedule_type']}: {sp['schedule_expr']}").classes("bg-blue-600")
                                ui.badge(sp["status"]).classes("bg-green-600" if sp["status"] == "active" else "bg-gray-500")
                                if sp["auto_approve"]:
                                    ui.badge("自动批准").classes("bg-red-600")
                                ui.label(f"下次 {sp['next_run_at'] or '—'}").classes("text-xs text-gray-400")
                            ui.label(sp["mat_text"][:80]).classes("text-sm")
                            with ui.row().classes("gap-2"):
                                if sp["status"] == "active":
                                    ui.button("暂停", on_click=lambda s=sp: (_set_status(s["id"], "paused"), render())).props("flat")
                                elif sp["status"] == "paused":
                                    ui.button("恢复", on_click=lambda s=sp: (_set_status(s["id"], "active"), render())).props("flat")
                                ui.button("删除", on_click=lambda s=sp: (_delete(s["id"]), render())).props("flat color=negative")

            render()

    def _edit(sp, refresh):
        with get_conn() as conn:
            accounts = conn.execute("SELECT id, handle FROM accounts ORDER BY id").fetchall()
            mats = conn.execute("SELECT id, text FROM materials WHERE kind='post' AND status='active' ORDER BY id").fetchall()
        if not accounts or not mats:
            ui.notify("需要至少一个账号和一条 active 的发帖素材", type="negative"); return

        with ui.dialog() as dialog, ui.card().classes("min-w-[520px]"):
            ui.label("新建定时计划").classes("text-lg font-bold")
            acc = ui.select({a["id"]: a["handle"] for a in accounts}, value=accounts[0]["id"], label="账号").props("outlined")
            mat = ui.select({m["id"]: m["text"][:40] for m in mats}, value=mats[0]["id"], label="发帖素材").props("outlined")
            stype = ui.select({"once": "一次性", "daily": "每天", "weekly": "每周", "cron": "cron(M H * * *)"},
                              value="daily", label="类型").props("outlined")
            expr = ui.input("表达式", value="21:00").classes("w-full").props("outlined")
            ui.label("提示：once→2026-09-01T21:00 · daily→21:00 · weekly→mon,thu 21:00 · cron→0 21 * * *").classes("text-xs text-gray-400")
            auto = ui.switch("自动批准（到点直接发送，不经人工审核）", value=False)

            def do_save():
                with get_conn() as conn:
                    acc_row = conn.execute("SELECT timezone FROM accounts WHERE id=?", (acc.value,)).fetchone()
                try:
                    nxt = compute_next_run(stype.value, expr.value.strip(), datetime.now(timezone.utc), acc_row["timezone"])
                except ValueError as e:
                    ui.notify(str(e), type="negative"); return
                with get_conn() as conn:
                    conn.execute(
                        "INSERT INTO scheduled_posts(account_id, material_id, schedule_type, schedule_expr, "
                        "next_run_at, auto_approve, status, created_at) VALUES (?,?,?,?,?,?,'active',?)",
                        (acc.value, mat.value, stype.value, expr.value.strip(),
                         nxt.strftime("%Y-%m-%dT%H:%M:%SZ") if nxt else None, 1 if auto.value else 0, utcnow_iso()))
                    conn.commit()
                dialog.close(); refresh(); ui.notify("已创建", type="positive")

            with ui.row():
                ui.button("保存", on_click=do_save).props("color=primary")
                ui.button("取消", on_click=dialog.close).props("flat")
        dialog.open()


def _set_status(sid: int, status: str):
    with get_conn() as conn:
        conn.execute("UPDATE scheduled_posts SET status=? WHERE id=?", (status, sid))
        conn.commit()


def _delete(sid: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM scheduled_posts WHERE id=?", (sid,))
        conn.commit()
