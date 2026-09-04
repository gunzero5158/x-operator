"""附件（配图 / 视频）的界面组件：上传框 + 缩略图条，素材库 / 审核队列 / AI 撰写 / 定时计划共用。"""
from __future__ import annotations

from pathlib import Path

from nicegui import ui

from ..core import media

_THUMB = {"sm": "w-14 h-14", "md": "w-24 h-24"}


def media_strip(files: list[str], size: str = "sm", on_remove=None) -> None:
    """一排缩略图。on_remove 不为空时每个附件带删除按钮。"""
    files = list(files or [])
    if not files:
        return
    box = _THUMB.get(size, _THUMB["sm"])
    with ui.row().classes("gap-2 items-center flex-wrap"):
        for rel in files:
            kind = media.media_kind(rel) or "image"
            exists = media.abs_path(rel).is_file()
            with ui.element("div").classes("relative"):
                if not exists:
                    with ui.element("div").classes(f"{box} rounded bg-red-100 flex items-center justify-center"):
                        ui.icon("broken_image", color="red").classes("text-2xl")
                    tip = "文件已丢失，发送会失败，请删掉重新上传"
                elif kind == "video":
                    with ui.element("div").classes(f"{box} rounded bg-slate-800 flex items-center justify-center"):
                        ui.icon("movie", color="white").classes("text-2xl")
                    tip = "视频：" + Path(rel).name
                else:
                    ui.image(media.url_for(rel)).classes(f"{box} rounded object-cover border")
                    tip = ("GIF：" if kind == "gif" else "图片：") + Path(rel).name
                ui.tooltip(tip)
                if on_remove is not None:
                    ui.button(icon="close", on_click=lambda r=rel: on_remove(r)) \
                        .props("round dense size=xs color=negative").classes("absolute -top-2 -right-2")


def media_badge(files: list[str]) -> None:
    """卡片标签行里的小标：「📎 2 张图片」。"""
    files = list(files or [])
    if not files:
        return
    lost = media.missing(files)
    ui.badge(("📎 " + media.describe(files)) + ("（文件丢失）" if lost else "")) \
        .classes("bg-pink-600" if not lost else "bg-red-600") \
        .tooltip("发送时会随正文一起上传这些附件" if not lost else "附件文件在 data/media 里找不到了，发送会失败")


class MediaField:
    """带上传的附件编辑区。用法：f = MediaField(initial)；保存时取 f.files。"""

    def __init__(self, initial: list[str] | None = None, label: str = "配图 / 视频（选填）", note: str = ""):
        self.files: list[str] = list(initial or [])
        self._initial = set(self.files)
        with ui.column().classes("w-full gap-1"):
            ui.label(label).classes("text-sm font-semibold")
            self.strip = ui.row().classes("gap-2 items-center flex-wrap min-h-4")
            self.upload = ui.upload(auto_upload=True, multiple=True, on_upload=self._on_upload,
                                    on_rejected=lambda e: ui.notify("文件被拒收：太大或类型不对。" + media.RULE_TEXT, type="negative", multi_line=True),
                                    max_file_size=media.VIDEO_MAX_BYTES,
                                    label="点这里选文件，或把图片 / 视频拖进来（上传完自动出现在上面）") \
                .props(f'accept="{media.ACCEPT}" flat bordered').classes("w-full")
            ui.label(media.RULE_TEXT + (" " + note if note else "")).classes("text-xs text-gray-400")
        self.render()

    def render(self) -> None:
        self.strip.clear()
        with self.strip:
            if not self.files:
                ui.label("没有附件（纯文字）").classes("text-xs text-gray-400")
            else:
                media_strip(self.files, size="md", on_remove=self.remove)
                ui.label(media.describe(self.files)).classes("text-xs text-gray-500")

    def remove(self, rel: str) -> None:
        if rel in self.files:
            self.files.remove(rel)
        # 本次对话框里刚传上来、还没保存过的文件，直接删掉；老文件可能别处还在用，留给启动时的孤儿清理
        if rel not in self._initial:
            media.delete_file(rel)
        self.render()

    async def _on_upload(self, e) -> None:
        name = e.file.name or "file"
        try:
            size = e.file.size()
        except Exception:
            size = 0
        err = media.check_one(name, size) or media.can_add(self.files, name)
        if err:
            ui.notify(err, type="negative", multi_line=True)
            self.upload.reset()
            return
        rel = media.new_rel_path(name)
        try:
            await e.file.save(media.abs_path(rel))
        except Exception as ex:
            ui.notify(f"保存文件失败：{ex}", type="negative")
            self.upload.reset()
            return
        self.files.append(rel)
        self.upload.reset()
        self.render()
        ui.notify(f"已添加 {Path(name).name}", type="positive")
