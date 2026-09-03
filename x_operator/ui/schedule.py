"""定时计划（design-v1.1 §8.6）：定时发帖计划的增删改、暂停/恢复、立即生成一次。"""
from __future__ import annotations

from datetime import datetime, timezone

from nicegui import ui

from ..core.schedule_calc import compute_next_run
from ..core.scheduler import enqueue_scheduled_post
from ..db.database import get_conn, to_iso, utcnow_iso
from .layout import confirm, fmt_time, shell

_TYPE_LABEL = {"once": "一次", "daily": "每天", "weekly": "每周", "cron": "cron"}
_STATUS_LABEL = {"active": "进行中", "paused": "已暂停", "done": "已完成", "missed": "已错过"}


def register(jobs) -> None:
    @ui.page("/schedule")
    def schedule_page():
        with shell("/schedule"):
            with ui.row().classes("items-center justify-between w-full"):
                ui.label("定时计划").classes("text-2xl font-bold")
                ui.button("新建计划", icon="add", on_click=lambda: _edit(None, render)).props("color=primary")
            ui.label("到点后计划会生成一条「发帖」条目到审核队列（勾了自动批准则直接进待发送），"
                     "再由分发器按账号活跃时段/间隔发出。后台每分钟检查一次到点计划。").classes("text-xs text-gray-400")

            body = ui.column().classes("w-full gap-2")

            async def delete(sp):
                if await confirm("删除这个定时计划？"):
                    _delete(sp["id"]); ui.notify("已删除", type="positive"); render()

            def fire_now(sp):
                n = _fire_now(sp["id"])
                ui.notify("已生成一条到审核队列" if n else "生成失败：素材不存在或已删除", type="positive" if n else "negative")
                render()

            def render():
                body.clear()
                with get_conn() as conn:
                    rows = conn.execute(
                        "SELECT sp.*, a.handle AS acc_handle, m.text AS mat_text, m.deleted_at AS mat_deleted "
                        "FROM scheduled_posts sp JOIN accounts a ON a.id=sp.account_id "
                        "JOIN materials m ON m.id=sp.material_id ORDER BY sp.id").fetchall()
                with body:
                    if not rows:
                        ui.label("暂无定时计划").classes("text-gray-400")
                        return
                    for sp in rows:
                        with ui.card().classes("w-full"):
                            with ui.row().classes("items-center gap-2"):
                                ui.badge(f"@{sp['acc_handle']}").classes("bg-slate-600")
                                ui.badge(f"{_TYPE_LABEL.get(sp['schedule_type'], sp['schedule_type'])}: {sp['schedule_expr']}").classes("bg-blue-600")
                                ui.badge(_STATUS_LABEL.get(sp["status"], sp["status"])).classes("bg-green-600" if sp["status"] == "active" else "bg-gray-500")
                                if sp["auto_approve"]:
                                    ui.badge("自动批准").classes("bg-red-600")
                                if sp["mat_deleted"]:
                                    ui.badge("素材已在回收站").classes("bg-amber-500")
                                ui.label(f"下次 {fmt_time(sp['next_run_at']) if sp['next_run_at'] else '—'}"
                                         + (f" · 上次 {fmt_time(sp['last_run_at'])}" if sp["last_run_at"] else "")).classes("text-xs text-gray-400")
                            ui.label(sp["mat_text"][:120]).classes("text-sm")
                            with ui.row().classes("gap-2"):
                                ui.button("编辑", on_click=lambda s=sp: _edit(s, render)).props("flat dense")
                                if sp["status"] == "active":
                                    ui.button("暂停", on_click=lambda s=sp: (_set_status(s["id"], "paused"), render())).props("flat dense")
                                elif sp["status"] in ("paused", "done", "missed"):
                                    ui.button("恢复/重新启用", on_click=lambda s=sp: (_reactivate(s), render())).props("flat dense")
                                ui.button("立即生成一次", icon="bolt", on_click=lambda s=sp: fire_now(s)).props("flat dense").tooltip("不等到点，现在就生成一条到审核队列")
                                ui.button("删除", icon="delete", on_click=lambda s=sp: delete(s)).props("flat dense color=negative")

            render()

    def _edit(sp, refresh):
        with get_conn() as conn:
            accounts = conn.execute("SELECT id, handle FROM accounts ORDER BY id").fetchall()
            mats = conn.execute("SELECT id, text FROM materials WHERE kind='post' AND status='active' AND deleted_at IS NULL ORDER BY id").fetchall()
            cur_mat = conn.execute("SELECT id, text, status, deleted_at FROM materials WHERE id=?", (sp["material_id"],)).fetchone() if sp else None
        if not accounts or not (mats or cur_mat):
            ui.notify("需要至少一个账号和一条「启用」状态的发帖素材（素材库 → 新建 → 类型选发帖）", type="negative"); return

        with ui.dialog() as dialog, ui.card().classes("min-w-[520px]"):
            ui.label("编辑定时计划" if sp else "新建定时计划").classes("text-lg font-bold")
            acc = ui.select({a["id"]: a["handle"] for a in accounts},
                            value=sp["account_id"] if sp else accounts[0]["id"], label="账号").classes("w-full").props("outlined")
            mat_opts = {m["id"]: m["text"][:40] for m in mats}
            if cur_mat is not None and cur_mat["id"] not in mat_opts:
                # 计划当前用的素材已归档/进回收站：仍然列出来并标明，不能悄悄换成别的
                tag = "已在回收站" if cur_mat["deleted_at"] else f"状态：{cur_mat['status']}"
                mat_opts = {cur_mat["id"]: f"⚠ {tag}｜{cur_mat['text'][:36]}", **mat_opts}
            mat_val = sp["material_id"] if sp else mats[0]["id"]
            mat = ui.select(mat_opts, value=mat_val, label="发帖素材").classes("w-full").props("outlined")
            stype = ui.select({"once": "一次性", "daily": "每天", "weekly": "每周", "cron": "cron(M H * * *)"},
                              value=sp["schedule_type"] if sp else "daily", label="类型").classes("w-full").props("outlined")
            expr = ui.input("表达式", value=sp["schedule_expr"] if sp else "21:00").classes("w-full").props("outlined")
            ui.label("提示：once→2026-09-01T21:00 · daily→21:00 · weekly→mon,thu 21:00 · cron→0 21 * * *（按账号时区）").classes("text-xs text-gray-400")
            auto = ui.switch("自动批准（到点直接进待发送，不经人工审核）", value=bool(sp["auto_approve"]) if sp else False)

            def do_save():
                with get_conn() as conn:
                    acc_row = conn.execute("SELECT timezone FROM accounts WHERE id=?", (acc.value,)).fetchone()
                try:
                    nxt = compute_next_run(stype.value, expr.value.strip(), datetime.now(timezone.utc), acc_row["timezone"])
                except ValueError as e:
                    ui.notify(str(e), type="negative"); return
                if nxt is None:
                    ui.notify("这个时间已经过去了，请填一个将来的时间", type="negative"); return
                nxt_s = to_iso(nxt)
                with get_conn() as conn:
                    m = conn.execute("SELECT 1 FROM materials WHERE id=? AND status='active' AND deleted_at IS NULL", (mat.value,)).fetchone()
                    if m is None:
                        ui.notify("所选素材不是「启用」状态或已在回收站，请换一条（或先到素材库恢复/启用它）", type="negative"); return
                    if sp:
                        conn.execute(
                            "UPDATE scheduled_posts SET account_id=?, material_id=?, schedule_type=?, schedule_expr=?, "
                            "next_run_at=?, auto_approve=?, status='active' WHERE id=?",
                            (acc.value, mat.value, stype.value, expr.value.strip(), nxt_s, 1 if auto.value else 0, sp["id"]))
                    else:
                        conn.execute(
                            "INSERT INTO scheduled_posts(account_id, material_id, schedule_type, schedule_expr, "
                            "next_run_at, auto_approve, status, created_at) VALUES (?,?,?,?,?,?,'active',?)",
                            (acc.value, mat.value, stype.value, expr.value.strip(), nxt_s, 1 if auto.value else 0, utcnow_iso()))
                    conn.commit()
                dialog.close(); refresh(); ui.notify("已保存，下次运行 " + fmt_time(nxt_s), type="positive")

            with ui.row():
                ui.button("保存", on_click=do_save).props("color=primary")
                ui.button("取消", on_click=dialog.close).props("flat")
        dialog.open()


def _set_status(sid: int, status: str):
    with get_conn() as conn:
        conn.execute("UPDATE scheduled_posts SET status=? WHERE id=?", (status, sid))
        conn.commit()


def _reactivate(sp) -> None:
    """恢复计划并重算下次时间；一次性且时间已过则提示。"""
    with get_conn() as conn:
        tz = conn.execute("SELECT timezone FROM accounts WHERE id=?", (sp["account_id"],)).fetchone()["timezone"]
    try:
        nxt = compute_next_run(sp["schedule_type"], sp["schedule_expr"], datetime.now(timezone.utc), tz)
    except ValueError as e:
        ui.notify(str(e), type="negative"); return
    if nxt is None:
        ui.notify("一次性计划的时间已过去，请「编辑」改成将来的时间", type="warning"); return
    with get_conn() as conn:
        conn.execute("UPDATE scheduled_posts SET status='active', next_run_at=? WHERE id=?", (to_iso(nxt), sp["id"]))
        conn.commit()
    ui.notify("已恢复，下次运行 " + fmt_time(to_iso(nxt)), type="positive")


def _fire_now(sid: int) -> int:
    """不等到点，现在就按计划生成一条（不改下次运行时间）。"""
    with get_conn() as conn:
        sp = conn.execute("SELECT * FROM scheduled_posts WHERE id=?", (sid,)).fetchone()
        if sp is None or not enqueue_scheduled_post(conn, sp):
            conn.rollback()
            return 0
        conn.execute("UPDATE scheduled_posts SET last_run_at=? WHERE id=?", (utcnow_iso(), sid))
        conn.commit()
    return 1


def _delete(sid: int):
    with get_conn() as conn:
        conn.execute("UPDATE review_queue SET scheduled_post_id=NULL WHERE scheduled_post_id=?", (sid,))
        conn.execute("DELETE FROM scheduled_posts WHERE id=?", (sid,))
        conn.commit()
