"""进程内的手动任务记录；页面只订阅进度，离开页面不会删除任务。"""
from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from threading import RLock
from uuid import uuid4
from typing import Callable

from nicegui import ui


@dataclass
class TaskProgress:
    label: str
    key: str = ""
    result_link: tuple[str, str] | None = None
    note: str = ""
    id: str = field(default_factory=lambda: uuid4().hex)
    started: float = field(default_factory=time.monotonic)
    finished: float | None = None
    fraction: float | None = None
    message: str = "准备中…"
    ok: bool = True
    result_action: Callable[[], None] | None = None

    def update(self, fraction: float, message: str) -> None:
        with _lock:
            self.fraction = max(0.0, min(1.0, float(fraction)))
            self.message = message

    def finish(self, message: str, ok: bool = True) -> None:
        with _lock:
            self.message, self.ok = message, ok
            self.finished = time.monotonic()
            self.fraction = 1.0


_lock = RLock()
_tasks: dict[str, TaskProgress] = {}


def start_task(label: str, *, key: str = "", result_link=None, note: str = "") -> TaskProgress | None:
    with _lock:
        if key and any(t.key == key and t.finished is None for t in _tasks.values()):
            return None
        # 只回收旧的已完成记录，运行中的任务始终保留。
        completed = [t.id for t in _tasks.values() if t.finished is not None]
        for tid in completed[:-29]:
            _tasks.pop(tid, None)
        task = TaskProgress(label, key=key, result_link=result_link, note=note)
        _tasks[task.id] = task
        return task


def _snapshot() -> list[TaskProgress]:
    with _lock:
        return [replace(t) for t in reversed(list(_tasks.values()))]


def _dismiss(tid: str) -> None:
    with _lock:
        task = _tasks.get(tid)
        if task and task.finished is not None:
            del _tasks[tid]


def mount_task_panel() -> None:
    """没有遮罩、不抢焦点；每个页面从同一份任务记录恢复显示。"""
    client = ui.context.client
    with client.content:
        with ui.card().classes("xo-task-panel fixed bottom-4 right-4 z-50") as panel:
            with ui.row().classes("xo-task-heading w-full items-center justify-between px-3 py-2 gap-2"):
                heading = ui.label("任务进度").classes("font-semibold text-sm")
                toggle = ui.button("收起", icon="expand_more").props("flat dense")
            details = ui.column().classes("xo-task-details w-full p-3 pt-0 gap-3 overflow-y-auto")
    initial_tasks = _snapshot()
    expanded = not initial_tasks or any(t.finished is None for t in initial_tasks)
    signature = None

    def sync_expanded():
        details.set_visibility(expanded)
        toggle.set_text("收起" if expanded else "展开")
        toggle.props("icon=expand_more" if expanded else "icon=expand_less")
        panel.classes(remove="is-collapsed" if expanded else "", add="" if expanded else "is-collapsed")

    def collapse():
        nonlocal expanded
        expanded = not expanded
        sync_expanded()

    toggle.on_click(collapse)
    sync_expanded()

    def tick():
        nonlocal signature
        tasks = _snapshot()
        panel.set_visibility(bool(tasks))
        running = sum(t.finished is None for t in tasks)
        heading.set_text(f"任务进度 · {running} 项进行中" if running else "任务进度 · 已完成")
        now = time.monotonic()
        current = [(t.id, t.message, t.fraction, t.finished, int((t.finished or now) - t.started)) for t in tasks]
        if signature == current or not expanded:
            return
        signature = current
        details.clear()
        with details:
            for task in tasks:
                active = task.finished is None
                with ui.column().classes("w-full gap-1 border-t border-slate-200 pt-2"):
                    with ui.row().classes("w-full items-center gap-2 no-wrap"):
                        if active:
                            ui.spinner(size="sm")
                        else:
                            ui.icon("check_circle" if task.ok else "warning").classes(
                                "text-green-600" if task.ok else "text-orange-600")
                        ui.label(task.label).classes("font-medium text-sm flex-1")
                        if not active:
                            ui.button(icon="close", on_click=lambda tid=task.id: _dismiss(tid)) \
                                .props('flat dense round aria-label="移除已完成任务"').tooltip("移除已完成任务")
                    if active:
                        bar = ui.linear_progress(task.fraction or 0, show_value=False).classes("w-full")
                        if task.fraction is None:
                            bar.props("indeterminate")
                    elapsed = int((task.finished or now) - task.started)
                    prefix = f"{int(task.fraction * 100)}% · " if active and task.fraction is not None else ""
                    ui.label(f"{prefix}{'已运行' if active else '用时'} {elapsed} 秒").classes("text-xs text-slate-500")
                    ui.label(task.message).classes("text-sm whitespace-pre-wrap break-words w-full")
                    if task.note and active:
                        ui.label(task.note).classes("text-xs text-slate-500")
                    if task.result_link:
                        title, href = task.result_link
                        ui.link(title, href).classes("text-sm")
                    if not active and task.result_action:
                        def show_result(action=task.result_action):
                            with client.content:
                                action()
                        ui.button("查看生成结果", on_click=show_result).props("outline dense")

    tick()
    ui.timer(0.5, tick)
