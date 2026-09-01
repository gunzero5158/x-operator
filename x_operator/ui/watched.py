"""监控推主（design-v1.1 §8.4）：添加/编辑/删除/启停被监控的推主，重置游标，运行一次监控。"""
from __future__ import annotations

from nicegui import run, ui

from ..adapters import factory
from ..adapters.base import XClientError
from ..core.monitor import get_primary_account, is_demo_id
from ..db.database import get_conn, utcnow_iso
from .layout import confirm, fmt_time, run_job, shell


def _resolve_and_add(handle: str, note: str, include_replies: bool) -> str:
    """解析 @handle → x_user_id 并入库。返回错误文案，空串表示成功。（阻塞：在线程池里跑）"""
    handle = handle.lstrip("@").strip()
    if not handle:
        return "请输入 @handle"
    account = get_primary_account()
    if account is None:
        return "没有状态为「启用」的账号，无法解析用户。请先到「设置 → 账号」添加"
    try:
        client = factory.get_client(account)
        user = client.get_user_by_handle(handle)
    except (XClientError, ValueError) as e:
        return f"解析 @{handle} 失败：{e}"
    with get_conn() as conn:
        try:
            conn.execute("INSERT INTO watched_users(handle, x_user_id, include_replies, note, created_at) VALUES (?,?,?,?,?)",
                         (user.handle, user.user_id, 1 if include_replies else 0, note.strip(), utcnow_iso()))
            conn.commit()
        except Exception:
            return f"@{user.handle} 已在监控列表中"
    return ""


def register(jobs) -> None:
    @ui.page("/watched")
    def watched_page():
        with shell("/watched"):
            with ui.row().classes("items-center justify-between w-full"):
                ui.label("监控推主").classes("text-2xl font-bold")
                ui.button("运行一次监控", icon="play_arrow", on_click=lambda: run_job(jobs.monitor.run_once, "监控", render)).props("outline")
            ui.label("添加时会通过你的账号去 X 查询该用户；每次运行监控拉取其最新推文 → 预检 → 匹配素材 → 进审核队列。"
                     " 抓到的推文（含被过滤的及原因）都在「抓取记录」页。").classes("text-xs text-gray-400")

            with ui.card().classes("w-full"):
                with ui.row().classes("items-end gap-2 w-full"):
                    inp = ui.input("@handle").props("outlined dense")
                    note_in = ui.input("备注（选填）").props("outlined dense").classes("flex-1")
                    incl = ui.switch("含回复", value=False)

                    async def add():
                        h = inp.value or ""
                        add_btn.disable()
                        try:
                            err = await run.io_bound(_resolve_and_add, h, note_in.value or "", bool(incl.value))
                        finally:
                            add_btn.enable()
                        if err:
                            ui.notify(err, type="negative", multi_line=True, close_button=True)
                        else:
                            ui.notify("已添加", type="positive"); inp.value = ""; note_in.value = ""; render()
                    add_btn = ui.button("添加", icon="add", on_click=add).props("color=primary")

            body = ui.column().classes("w-full gap-2")

            async def delete(u):
                if await confirm(f"删除监控推主 @{u['handle']}？", "已抓取的记录会保留。"):
                    _delete(u["id"]); ui.notify("已删除", type="positive"); render()

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
                                    with ui.row().classes("items-center gap-2"):
                                        ui.label(f"@{u['handle']}").classes("font-semibold")
                                        if is_demo_id(u["x_user_id"]):
                                            ui.badge("旧版演示数据").classes("bg-amber-500").tooltip("旧版本留下的假推主，监控会跳过；请删掉后重新添加")
                                        if u["include_replies"]:
                                            ui.badge("含回复").classes("bg-slate-500")
                                        if not u["enabled"]:
                                            ui.badge("已停用").classes("bg-gray-400")
                                    ui.label(f"命中 {u['hit_count']} 次 · 游标 {u['last_seen_tweet_id'] or '未设置（下次抓最近几条）'}"
                                             + f" · 添加于 {fmt_time(u['created_at'])}"
                                             + (f" · {u['note']}" if u['note'] else "")).classes("text-xs text-gray-400")
                                with ui.row().classes("items-center gap-1"):
                                    sw = ui.switch("启用", value=bool(u["enabled"]))
                                    sw.on("update:model-value", lambda e, uid=u["id"]: _toggle(uid, e.args))
                                    ui.button("编辑", on_click=lambda uu=u: _edit(uu, render)).props("flat dense")
                                    ui.button("重置游标", on_click=lambda uid=u["id"]: (_reset_cursor(uid), ui.notify("已重置，下次监控会重新抓最近几条", type="info"), render())).props("flat dense").tooltip("清掉「上次看到哪条」的记录，下次监控重新抓最近几条")
                                    ui.button("删除", icon="delete", on_click=lambda uu=u: delete(uu)).props("flat dense color=negative")

            render()

    def _edit(u, refresh):
        with ui.dialog() as dialog, ui.card().classes("min-w-96"):
            ui.label(f"编辑 @{u['handle']}").classes("text-lg font-bold")
            note = ui.input("备注", value=u["note"] or "").classes("w-full").props("outlined")
            incl = ui.switch("监控时包含其回复", value=bool(u["include_replies"]))

            def save():
                with get_conn() as conn:
                    conn.execute("UPDATE watched_users SET note=?, include_replies=? WHERE id=?",
                                 (note.value.strip(), 1 if incl.value else 0, u["id"]))
                    conn.commit()
                dialog.close(); refresh(); ui.notify("已保存", type="positive")

            with ui.row():
                ui.button("保存", on_click=save).props("color=primary")
                ui.button("取消", on_click=dialog.close).props("flat")
        dialog.open()


def _toggle(uid: int, enabled):
    with get_conn() as conn:
        conn.execute("UPDATE watched_users SET enabled=? WHERE id=?", (1 if enabled else 0, uid))
        conn.commit()


def _reset_cursor(uid: int):
    with get_conn() as conn:
        conn.execute("UPDATE watched_users SET last_seen_tweet_id=NULL WHERE id=?", (uid,))
        conn.commit()


def _delete(uid: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM watched_users WHERE id=?", (uid,))
        conn.commit()
