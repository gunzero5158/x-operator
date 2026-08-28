"""素材库（design-v1.1 §8.3）：回复/发帖素材的增删改、启用/归档、回收站。

删除 = 软删除进回收站（deleted_at 打时间戳），回收站里可恢复或彻底删除。
进回收站的素材不会再被匹配引擎选中；引用它的定时计划到点时会自动暂停。
"""
from __future__ import annotations

import sqlite3

from nicegui import ui

from ..db.database import get_conn, utcnow_iso
from .layout import confirm, fmt_time, shell


def _load(kind_filter: str, status_filter: str, trash: bool):
    q = "SELECT * FROM materials WHERE deleted_at IS " + ("NOT NULL" if trash else "NULL")
    args: list = []
    if kind_filter != "all":
        q += " AND kind=?"; args.append(kind_filter)
    if status_filter != "all" and not trash:
        q += " AND status=?"; args.append(status_filter)
    q += " ORDER BY deleted_at DESC, COALESCE(translation_group_id, id), id" if trash \
        else " ORDER BY COALESCE(translation_group_id, id), id"
    with get_conn() as conn:
        return conn.execute(q, args).fetchall()


def _trash_count() -> int:
    with get_conn() as conn:
        return conn.execute("SELECT COUNT(*) AS c FROM materials WHERE deleted_at IS NOT NULL").fetchone()["c"]


def _save(mid, kind, text, lang, tags, status):
    with get_conn() as conn:
        if mid:
            conn.execute("UPDATE materials SET kind=?, text=?, lang=?, scenario_tags=?, status=? WHERE id=?",
                         (kind, text, lang, tags, status, mid))
        else:
            conn.execute("INSERT INTO materials(kind, text, lang, scenario_tags, status, created_by) "
                         "VALUES (?,?,?,?,?,'human')", (kind, text, lang, tags, status))
        conn.commit()


def _set_status(mid, status):
    with get_conn() as conn:
        conn.execute("UPDATE materials SET status=? WHERE id=?", (status, mid))
        conn.commit()


def _soft_delete(mid: int) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE materials SET deleted_at=? WHERE id=?", (utcnow_iso(), mid))
        conn.commit()


def _restore(mid: int) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE materials SET deleted_at=NULL WHERE id=?", (mid,))
        conn.commit()


def _hard_delete(mid: int) -> str:
    """彻底删除。被定时计划引用则拒绝（返回原因）；审核队列里的引用置空后删除。"""
    with get_conn() as conn:
        n = conn.execute("SELECT COUNT(*) AS c FROM scheduled_posts WHERE material_id=?", (mid,)).fetchone()["c"]
        if n:
            return f"有 {n} 个定时计划引用这条素材，请先到「定时计划」删除对应计划"
        try:
            conn.execute("UPDATE review_queue SET material_id=NULL WHERE material_id=?", (mid,))
            conn.execute("UPDATE materials SET translation_group_id=NULL WHERE translation_group_id=?", (mid,))
            conn.execute("DELETE FROM materials WHERE id=?", (mid,))
            conn.commit()
        except sqlite3.IntegrityError as e:
            conn.rollback()
            return f"删除失败：{e}"
    return ""


def _empty_trash() -> tuple[int, int]:
    """清空回收站，返回 (删除数, 因被引用而保留数)。"""
    with get_conn() as conn:
        ids = [r["id"] for r in conn.execute("SELECT id FROM materials WHERE deleted_at IS NOT NULL").fetchall()]
    done = kept = 0
    for mid in ids:
        if _hard_delete(mid):
            kept += 1
        else:
            done += 1
    return done, kept


def register(jobs) -> None:
    @ui.page("/materials")
    def materials_page():
        with shell("/materials"):
            state = {"trash": False}
            with ui.row().classes("items-center justify-between w-full"):
                title = ui.label("素材库").classes("text-2xl font-bold")
                with ui.row().classes("items-center gap-2"):
                    kind_f = ui.select({"all": "全部类型", "reply": "回复", "post": "发帖"}, value="all").props("dense outlined")
                    status_f = ui.select({"all": "全部状态", "active": "启用", "draft": "草稿", "archived": "归档"},
                                         value="all").props("dense outlined")
                    trash_btn = ui.button("回收站", icon="delete_sweep", on_click=lambda: toggle_trash()).props("outline")
                    new_btn = ui.button("新建素材", icon="add", on_click=lambda: _edit_dialog(None, render)).props("color=primary")

            hint = ui.label("").classes("text-xs text-gray-400")
            body = ui.column().classes("w-full gap-2")

            def toggle_trash():
                state["trash"] = not state["trash"]
                render()

            async def empty_trash():
                n = _trash_count()
                if not n:
                    ui.notify("回收站是空的", type="info"); return
                if await confirm(f"清空回收站？", f"将彻底删除 {n} 条素材，无法恢复。", ok_label="清空"):
                    done, kept = _empty_trash()
                    msg = f"已彻底删除 {done} 条"
                    if kept:
                        msg += f"，{kept} 条因被定时计划引用而保留"
                    ui.notify(msg, type="positive")
                    render()

            async def hard_delete(mid: int):
                if await confirm("彻底删除这条素材？", "删除后无法恢复。", ok_label="彻底删除"):
                    err = _hard_delete(mid)
                    ui.notify(err or "已彻底删除", type="negative" if err else "positive")
                    render()

            def render():
                body.clear()
                trash = state["trash"]
                tc = _trash_count()
                trash_btn.text = f"回收站（{tc}）" if not trash else "返回素材列表"
                if trash:
                    trash_btn.props("color=negative")
                else:
                    trash_btn.props(remove="color=negative")
                title.text = "素材库 · 回收站" if trash else "素材库"
                status_f.set_visibility(not trash)
                new_btn.set_visibility(not trash)
                hint.text = ("回收站里的素材不会被匹配引擎使用；可恢复或彻底删除。" if trash
                             else "「删除」会移入回收站（可恢复）；「归档」保留但不再参与匹配。")
                rows = _load(kind_f.value, status_f.value, trash)
                with body:
                    if trash and tc:
                        with ui.row().classes("w-full justify-end"):
                            ui.button("清空回收站", icon="delete_forever", on_click=empty_trash).props("color=negative outline dense")
                    if not rows:
                        ui.label("回收站是空的" if trash else "暂无素材").classes("text-gray-400")
                        return
                    for m in rows:
                        with ui.card().classes("w-full" + (" bg-red-50" if trash else "")):
                            with ui.row().classes("items-center gap-2"):
                                ui.badge("回复" if m["kind"] == "reply" else "发帖").classes("bg-blue-600")
                                ui.badge(m["lang"]).classes("bg-slate-500")
                                ui.badge({"active": "启用", "draft": "草稿", "archived": "归档"}.get(m["status"], m["status"])) \
                                    .classes("bg-green-600" if m["status"] == "active" else "bg-gray-500")
                                if m["created_by"] == "ai":
                                    ui.badge("AI").classes("bg-purple-600")
                                if m["translation_group_id"]:
                                    ui.badge(f"翻译组 #{m['translation_group_id']}").classes("bg-teal-600")
                                ui.label(f"用 {m['usage_count']} 次").classes("text-xs text-gray-400")
                                if m["scenario_tags"]:
                                    ui.label("#" + m["scenario_tags"]).classes("text-xs text-gray-400")
                                if trash:
                                    ui.label(f"删除于 {fmt_time(m['deleted_at'])}").classes("text-xs text-red-400")
                            ui.label(m["text"]).classes("text-sm whitespace-pre-wrap")
                            with ui.row().classes("gap-2"):
                                if trash:
                                    ui.button("恢复", icon="restore", on_click=lambda mm=m: (_restore(mm["id"]), ui.notify("已恢复", type="positive"), render())).props("flat")
                                    ui.button("彻底删除", icon="delete_forever", on_click=lambda mm=m: hard_delete(mm["id"])).props("flat color=negative")
                                else:
                                    ui.button("编辑", on_click=lambda mm=m: _edit_dialog(mm, render)).props("flat")
                                    if m["status"] == "active":
                                        ui.button("归档", on_click=lambda mm=m: (_set_status(mm["id"], "archived"), render())).props("flat")
                                    else:
                                        ui.button("启用", on_click=lambda mm=m: (_set_status(mm["id"], "active"), render())).props("flat")
                                    ui.button("删除", icon="delete", on_click=lambda mm=m: (_soft_delete(mm["id"]), ui.notify("已移入回收站", type="info"), render())).props("flat color=negative")

            kind_f.on("update:model-value", lambda e: render())
            status_f.on("update:model-value", lambda e: render())
            render()

    def _edit_dialog(m, refresh):
        with ui.dialog() as dialog, ui.card().classes("min-w-96"):
            ui.label("编辑素材" if m else "新建素材").classes("text-lg font-bold")
            kind = ui.select({"reply": "回复", "post": "发帖"}, value=m["kind"] if m else "reply", label="类型").props("outlined")
            lang = ui.select({"ja": "日语", "en": "英语", "zh": "中文"}, value=m["lang"] if m else "ja", label="语言").props("outlined")
            text = ui.textarea("正文", value=m["text"] if m else "").classes("w-full").props("outlined autogrow")
            tags = ui.input("场景标签（逗号分隔）", value=m["scenario_tags"] if m else "").classes("w-full").props("outlined")
            status = ui.select({"draft": "草稿", "active": "启用", "archived": "归档"},
                               value=m["status"] if m else "active", label="状态").props("outlined")

            def do_save():
                if not text.value.strip():
                    ui.notify("正文不能为空", type="negative"); return
                _save(m["id"] if m else None, kind.value, text.value.strip(), lang.value,
                      tags.value.strip(), status.value)
                dialog.close(); refresh(); ui.notify("已保存", type="positive")

            with ui.row():
                ui.button("保存", on_click=do_save).props("color=primary")
                ui.button("取消", on_click=dialog.close).props("flat")
        dialog.open()
