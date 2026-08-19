"""素材库（design-v1.1 §8.3）：回复/发帖素材的增删改与启用/归档。

MVP 聚焦文本素材 CRUD（媒体、翻译组并排、AI 撰写为后续增强）。
"""
from __future__ import annotations

from nicegui import ui

from ..db.database import get_conn, utcnow_iso
from .layout import shell


def _load(kind_filter: str, status_filter: str):
    q = "SELECT * FROM materials WHERE 1=1"
    args = []
    if kind_filter != "all":
        q += " AND kind=?"; args.append(kind_filter)
    if status_filter != "all":
        q += " AND status=?"; args.append(status_filter)
    q += " ORDER BY COALESCE(translation_group_id, id), id"
    with get_conn() as conn:
        return conn.execute(q, args).fetchall()


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


def register(jobs) -> None:
    @ui.page("/materials")
    def materials_page():
        with shell("/materials"):
            with ui.row().classes("items-center justify-between w-full"):
                ui.label("素材库").classes("text-2xl font-bold")
                with ui.row():
                    kind_f = ui.select({"all": "全部类型", "reply": "回复", "post": "发帖"}, value="all").props("dense outlined")
                    status_f = ui.select({"all": "全部状态", "active": "启用", "draft": "草稿", "archived": "归档"},
                                         value="all").props("dense outlined")
                    ui.button("新建素材", on_click=lambda: _edit_dialog(None, render))

            body = ui.column().classes("w-full gap-2")

            def render():
                body.clear()
                rows = _load(kind_f.value, status_f.value)
                with body:
                    if not rows:
                        ui.label("暂无素材").classes("text-gray-400")
                        return
                    for m in rows:
                        with ui.card().classes("w-full"):
                            with ui.row().classes("items-center gap-2"):
                                ui.badge(m["kind"]).classes("bg-blue-600")
                                ui.badge(m["lang"]).classes("bg-slate-500")
                                ui.badge(m["status"]).classes("bg-green-600" if m["status"] == "active" else "bg-gray-500")
                                if m["created_by"] == "ai":
                                    ui.badge("AI").classes("bg-purple-600")
                                ui.label(f"用 {m['usage_count']} 次").classes("text-xs text-gray-400")
                                if m["scenario_tags"]:
                                    ui.label("#" + m["scenario_tags"]).classes("text-xs text-gray-400")
                            ui.label(m["text"]).classes("text-sm")
                            with ui.row().classes("gap-2"):
                                ui.button("编辑", on_click=lambda mm=m: _edit_dialog(mm, render)).props("flat")
                                if m["status"] == "active":
                                    ui.button("归档", on_click=lambda mm=m: (_set_status(mm["id"], "archived"), render())).props("flat")
                                else:
                                    ui.button("启用", on_click=lambda mm=m: (_set_status(mm["id"], "active"), render())).props("flat")

            kind_f.on("update:model-value", lambda e: render())
            status_f.on("update:model-value", lambda e: render())
            render()

    def _edit_dialog(m, refresh):
        with ui.dialog() as dialog, ui.card().classes("min-w-96"):
            ui.label("编辑素材" if m else "新建素材").classes("text-lg font-bold")
            kind = ui.select({"reply": "回复", "post": "发帖"}, value=m["kind"] if m else "reply").props("outlined")
            lang = ui.select({"ja": "日语", "en": "英语", "zh": "中文"}, value=m["lang"] if m else "ja").props("outlined")
            text = ui.textarea("正文", value=m["text"] if m else "").classes("w-full").props("outlined autogrow")
            tags = ui.input("场景标签（逗号分隔）", value=m["scenario_tags"] if m else "").classes("w-full").props("outlined")
            status = ui.select({"draft": "草稿", "active": "启用", "archived": "归档"},
                               value=m["status"] if m else "active").props("outlined")

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
