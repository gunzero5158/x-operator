"""附件（配图 / 视频）的界面组件：上传框 + 缩略图条，素材库 / 任务队列 / AI 撰写 / 定时发帖计划共用。"""
from __future__ import annotations

import asyncio
from pathlib import Path
from collections import Counter

from .i18n import t as _tr, localized_props

from nicegui import run, ui

from ..core import media, thumbnails
from .layout import TAG

_THUMB = {"sm": "xo-media-sm", "md": "xo-media-md"}


def describe_media(files: list[str]) -> str:
    counts = Counter(media.media_kind(file) or "image" for file in files)
    messages = {"image": "{count} 张图片", "gif": "{count} 个 GIF", "video": "{count} 个视频"}
    return " + ".join(_tr(messages[kind], count=counts[kind]) for kind in messages if counts[kind])


def _preview_video(rel: str) -> None:
    with ui.dialog() as dialog, ui.card().classes("xo-video-dialog"):
        with ui.row().classes("w-full items-center justify-between"):
            ui.label(_tr('视频预览')).classes("text-lg font-semibold")
            ui.button(icon="close", on_click=dialog.close).props(localized_props('flat round aria-label="关闭视频预览"'))
        player = ui.video(media.url_for(rel)).classes("w-full xo-video-player").props('preload=metadata playsinline')
        with ui.row().classes("w-full items-center justify-between"):
            ui.label(Path(rel).name).classes("text-xs text-gray-500 break-all")
            ui.link(_tr('打开原视频'), media.url_for(rel), new_tab=True).classes("text-sm")
    def close_preview():
        player.pause()
        dialog.delete()
    dialog.on("hide", close_preview)
    dialog.open()


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
                    with ui.element("div").classes(f"xo-media-thumb {box} rounded bg-red-100 flex items-center justify-center"):
                        ui.icon("broken_image", color="red").classes("text-2xl")
                    tip = _tr('文件已丢失，发送会失败，请删掉重新上传')
                elif kind == "video":
                    with ui.element("button").classes(f"xo-media-thumb xo-video-thumb {box}") \
                            .props(localized_props('type=button aria-label="预览视频"')).on("click", lambda r=rel: _preview_video(r)):
                        fallback = ui.label(_tr('加载预览…')).classes("xo-video-fallback")
                        image = ui.image(thumbnails.thumbnail_url(rel)).classes("xo-video-poster") \
                            .props(localized_props('fit=contain loading=lazy no-spinner no-transition alt="视频画面缩略图"'))
                        def failed_preview(_, img=image, label=fallback):
                            img.set_visibility(False)
                            label.set_text(_tr('预览不可用'))
                        image.on("error", failed_preview)
                        image.on("load", lambda _, label=fallback: label.set_visibility(False))
                        with ui.element("span").classes("xo-video-play"):
                            ui.icon("play_arrow")
                        ui.label(_tr('视频')).classes("xo-video-badge")
                    tip = _tr('点击预览视频：') + Path(rel).name
                else:
                    ui.image(media.url_for(rel)).classes(f"xo-media-thumb {box} rounded object-cover border")
                    tip = ("GIF：" if kind == "gif" else _tr('图片：')) + Path(rel).name
                ui.tooltip(tip)
                if on_remove is not None:
                    ui.button(icon="close", on_click=lambda r=rel: on_remove(r)) \
                        .props(localized_props('round dense size=xs color=negative aria-label="移除附件"')).classes("absolute -top-2 -right-2")


def media_badge(files: list[str]) -> None:
    """卡片标签行里的小标：「📎 2 张图片」。"""
    files = list(files or [])
    if not files:
        return
    lost = media.missing(files)
    ui.badge(("📎 " + describe_media(files)) + (_tr('（文件丢失）') if lost else ""), color=None) \
        .classes(TAG["media"] if not lost else TAG["bad"]) \
        .tooltip(_tr('发送时会随正文一起上传这些附件') if not lost else _tr('附件文件在 data/media 里找不到了，发送会失败'))


class MediaField:
    """带上传的附件编辑区。用法：f = MediaField(initial)；保存时取 f.files。"""

    def __init__(self, initial: list[str] | None = None, label: str = _tr('配图 / 视频（选填）'), note: str = "",
                 max_items: int = media.MAX_ITEMS):
        self.files: list[str] = list(initial or [])
        self._initial = set(self.files)
        self.max_items = max_items
        self._save_lock = asyncio.Lock()
        self._pending_uploads = 0
        self._transferring = False
        with ui.column().classes("xo-media-field w-full gap-2"):
            self.title = ui.label(_tr(label)).classes("text-sm font-semibold")
            self.strip = ui.row().classes("gap-2 items-center flex-wrap min-h-4")
            self.upload = ui.upload(auto_upload=True, multiple=True, on_upload=self._on_upload,
                                    on_begin_upload=self._on_begin_upload,
                                    on_rejected=lambda e: ui.notify(_tr('文件被拒收：太大或类型不对。') +
                                                                   (media.POOL_RULE_TEXT if self.max_items == media.POOL_MAX_ITEMS else media.RULE_TEXT),
                                                                   type="negative", multi_line=True),
                                    max_file_size=media.VIDEO_MAX_BYTES,
                                    label=_tr('点这里选文件，或把图片 / 视频拖进来（上传完自动出现在上面）')) \
                .props(f'accept="{media.ACCEPT}" flat bordered').classes("w-full")
            self.upload.on("start", self._on_begin_upload)
            self.upload.on("finish", self._on_finish_upload)
            self.upload.on("failed", lambda: ui.notify(_tr('有文件未上传成功，请在上传框中重试失败的文件'), type="negative"), args=[])
            self.progress = ui.label().classes("text-xs text-orange-600")
            self.progress.set_visibility(False)
            self.note = ui.label().classes("xo-hint-short text-xs text-gray-400")
        self.set_limit(max_items, note)

    def set_limit(self, max_items: int, note: str = "", label: str | None = None) -> None:
        """切换数量上限和下面的说明（定时发帖在「固定附件 / 附件素材池」之间切换时用）。已超上限的文件不动，保存时再拦。"""
        self.max_items = max_items
        rule = media.RULE_TEXT if max_items == media.MAX_ITEMS else media.POOL_RULE_TEXT
        self.note.set_text(_tr(rule) + (" " + _tr(note) if note else ""))
        if label is not None:
            self.title.set_text(_tr(label))
        self.render()

    def render(self) -> None:
        self.strip.clear()
        with self.strip:
            if not self.files:
                ui.label(_tr('没有附件（纯文字）')).classes("text-xs text-gray-400")
            else:
                media_strip(self.files, size="md", on_remove=self.remove)
                ui.label(_tr('{p0}（{p1}/{p2} 个）', p0=describe_media(self.files), p1=len(self.files), p2=self.max_items)).classes("text-xs text-gray-500")

    @property
    def is_uploading(self) -> bool:
        return self._transferring or self._pending_uploads > 0

    def ready(self) -> bool:
        """保存表单前检查，避免把只收到一部分附件的状态写入数据库。"""
        if self.is_uploading:
            ui.notify(_tr('附件还在上传或保存，请等上传完成后再保存'), type="warning")
            return False
        return True

    def _show_progress(self) -> None:
        if self.progress.is_deleted:
            return
        self.progress.set_visibility(self.is_uploading)
        self.progress.set_text(_tr('附件处理中 · 已添加 {p0} 个，待保存 {p1} 个；完成前请勿关闭编辑窗口', p0=len(self.files), p1=self._pending_uploads))

    def _on_begin_upload(self) -> None:
        self._transferring = True
        self._show_progress()

    def _on_finish_upload(self) -> None:
        self._transferring = False
        # reset() 会 abort 整个队列；这里只移除已上传记录，不碰待传或正在传的文件。
        self.upload.run_method("removeUploadedFiles")
        self._show_progress()

    def remove(self, rel: str) -> None:
        if rel in self.files:
            self.files.remove(rel)
        # 本次对话框里刚传上来、还没保存过的文件，直接删掉；老文件可能别处还在用，留给启动时的孤儿清理
        if rel not in self._initial:
            media.delete_file(rel)
        self.render()

    def _on_upload(self, e):
        # NiceGUI 会调度异步处理器；先同步计数，finish 事件才能看到尚未落盘的文件。
        self._pending_uploads += 1
        self._show_progress()
        return self._save_upload(e)

    async def _save_upload(self, e) -> None:
        try:
            # 文件可以并发传输，但本组件的校验、落盘和追加列表按顺序执行。
            async with self._save_lock:
                if not self.strip.is_deleted:
                    await self._store_upload(e)
        finally:
            self._pending_uploads -= 1
            self._show_progress()

    async def _store_upload(self, e) -> None:
        name = e.file.name or "file"
        try:
            size = e.file.size()
        except Exception:
            size = 0
        err = media.check_one(name, size) or media.can_add(self.files, name, self.max_items)
        if err:
            ui.notify(f"{Path(name).name}：{err}", type="negative", multi_line=True)
            return
        tmp = media.new_tmp_path(name)
        try:
            await e.file.save(tmp)
            rel, reused = await run.io_bound(media.commit_upload, tmp, name)
        except Exception as ex:
            tmp.unlink(missing_ok=True)
            ui.notify(_tr('保存 {p0} 失败：{p1}', p0=Path(name).name, p1=ex), type="negative")
            return
        if self.strip.is_deleted:
            return
        if rel in self.files:
            ui.notify(_tr('{p0} 已经在附件里了（内容相同），没有重复添加', p0=Path(name).name), type="info", multi_line=True)
            return
        if reused:
            self._initial.add(rel)   # 复用的是别处也在用的文件，从这里移除时不能删盘上的文件
        # 落盘期间用户可能把素材池改成固定附件，按最新上限再核对一次。
        err = media.can_add(self.files, name, self.max_items)
        if err:
            ui.notify(_tr('{p0}：{p1}，未加入附件', p0=Path(name).name, p1=err), type="negative", multi_line=True)
            return
        self.files.append(rel)
        self.render()
        ui.notify(_tr('已添加 {p0}', p0=Path(name).name) + (_tr('（和已有附件内容相同，直接复用，不重复占空间）') if reused else ""),
                  type="positive", multi_line=reused)
