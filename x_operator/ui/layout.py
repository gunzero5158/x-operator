"""公共页面外壳（design-v1.1 §8.0）：深色顶栏 + 醒目导航 + 内容插槽，以及各页共用的小工具。"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Callable

from nicegui import run, ui

from ..db.database import get_conn

# (路径, 名称, material 图标)
NAV = [
    ("/", "仪表盘", "dashboard"),
    ("/queue", "审核队列", "rate_review"),
    ("/targets", "抓取记录", "travel_explore"),
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
    with ui.header().classes("items-center justify-between bg-slate-900 text-white px-4 py-2 shadow-lg gap-3"):
        # 左：品牌
        with ui.row().classes("items-center gap-2 shrink-0"):
            ui.icon("smart_toy").classes("text-2xl text-sky-400")
            ui.label("x-operator").classes("text-lg font-bold")

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


# ------------------------------------------------------------------------------------
# 各页共用的小工具
# ------------------------------------------------------------------------------------
async def confirm(title: str, detail: str = "", ok_label: str = "确认删除", color: str = "negative") -> bool:
    """弹确认框，返回用户是否点了确认。"""
    with ui.dialog() as dlg, ui.card().classes("min-w-80"):
        ui.label(title).classes("text-lg font-bold")
        if detail:
            ui.label(detail).classes("text-sm text-gray-500")
        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("取消", on_click=lambda: dlg.submit(False)).props("flat")
            ui.button(ok_label, on_click=lambda: dlg.submit(True)).props(f"color={color}")
    dlg.open()
    return bool(await dlg)


def notify_long(msg: str, ok: bool = True, kind: str | None = None) -> None:
    """可能较长的运行结果提示：多行 + 可关闭 + 停留久一点。"""
    ui.notify(msg, type=kind or ("positive" if ok else "warning"),
              multi_line=True, close_button=True, timeout=10000)


async def run_job(fn: Callable[[], Any], label: str, refresh: Callable[[], None] | None = None,
                  result_link: tuple[str, str] | None = None):
    """在线程池里跑阻塞的 job（抓取/LLM/发送可能要几十秒），不卡住页面；完成后弹结果。

    result_link=(按钮文字, 路径) 时改为弹对话框，带一个「去看结果」按钮（比一闪而过的提示更好找）。"""
    try:
        res = await run.io_bound(fn)
    except Exception as e:
        ui.notify(f"{label}出错：{e}", type="negative", multi_line=True, close_button=True, timeout=12000)
        if refresh:
            refresh()
        return None
    ok = getattr(res, "ok", True)
    msg = res.as_msg() if hasattr(res, "as_msg") else f"{label}完成"
    if result_link:
        text, href = result_link
        with ui.dialog() as dlg, ui.card().classes("min-w-96 max-w-[90vw]"):
            with ui.row().classes("items-center gap-2"):
                ui.icon("check_circle" if ok else "warning").classes("text-2xl " + ("text-green-600" if ok else "text-orange-500"))
                ui.label(f"{label}结果").classes("text-lg font-bold")
            ui.label(msg).classes("text-sm whitespace-pre-wrap")
            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("关闭", on_click=dlg.close).props("flat")
                ui.button(text, icon="arrow_forward", on_click=lambda: ui.navigate.to(href)).props("color=primary")
        dlg.open()
    else:
        notify_long(msg, ok=ok)
    if refresh:
        refresh()
    return res


async def run_job_with_progress(fn: Callable[..., Any], label: str, refresh: Callable[[], None] | None = None,
                                result_link: tuple[str, str] | None = None):
    """跟 run_job 一样在线程池里跑，但先弹一个进度框：fn 必须接受 progress 参数，
    在工作线程里调 progress(0~1 的进度, 当前在做什么) 就会实时显示。跑完进度框变成结果框。"""
    state = {"frac": 0.0, "text": "准备中…"}

    def progress(frac: float, text: str) -> None:
        state["frac"] = max(0.0, min(1.0, float(frac)))
        state["text"] = text

    with ui.dialog().props("persistent") as dlg, ui.card().classes("min-w-[480px] max-w-[90vw]"):
        with ui.row().classes("items-center gap-2"):
            icon = ui.spinner(size="lg")
            title = ui.label(f"{label}进行中…").classes("text-lg font-bold")
        bar = ui.linear_progress(0.0, show_value=False, size="12px").classes("w-full")
        pct = ui.label("0%").classes("text-xs text-gray-400")
        info = ui.label(state["text"]).classes("text-sm text-gray-600 whitespace-pre-wrap")
        result_box = ui.column().classes("w-full")
        btn_row = ui.row().classes("w-full justify-end gap-2")
    dlg.open()

    def tick():
        bar.value = state["frac"]
        pct.text = f"{int(state['frac'] * 100)}%"
        info.text = state["text"]
    timer = ui.timer(0.25, tick)

    try:
        res = await run.io_bound(fn, progress)
        ok = getattr(res, "ok", True)
        msg = res.as_msg() if hasattr(res, "as_msg") else f"{label}完成"
    except Exception as e:
        res, ok, msg = None, False, f"{label}出错：{e}"
    timer.cancel()
    state["frac"], state["text"] = 1.0, "完成"
    tick()
    icon.delete()
    title.text = f"{label}结果"
    with result_box:
        with ui.row().classes("items-center gap-2 no-wrap"):
            ui.icon("check_circle" if ok else "warning").classes("text-2xl " + ("text-green-600" if ok else "text-orange-500"))
            ui.label(msg).classes("text-sm whitespace-pre-wrap")
    with btn_row:
        ui.button("关闭", on_click=dlg.close).props("flat")
        if result_link:
            text, href = result_link
            ui.button(text, icon="arrow_forward", on_click=lambda: ui.navigate.to(href)).props("color=primary")
    if refresh:
        refresh()
    return res


def tweet_link(author_handle: str | None, tweet_id: str | None):
    """指向 X 上原推的链接。"""
    if not tweet_id:
        return
    if not str(tweet_id).isdigit():
        ui.label(f"推文 id {tweet_id}（非真实链接）").classes("text-xs text-gray-400")
    else:
        ui.link("在 X 上打开原推 ↗", f"https://x.com/{author_handle or 'i'}/status/{tweet_id}",
                new_tab=True).classes("text-xs")


TARGET_STATUS_LABEL = {
    "new": "待匹配",
    "queued": "已进审核队列",
    "no_match": "达标但未生成回复",
    "filtered": "已过滤/未达标",
    "expired": "已过期",
}

QUEUE_STATUS_LABEL = {
    "pending": "待审核", "approved": "待发送", "sending": "发送中", "sent": "已发送",
    "failed": "失败", "skipped": "已跳过", "expired": "已过期",
}


def fmt_time(iso: str | None) -> str:
    """UTC ISO → 本地易读（浏览器所在时区不可知，这里按账号常用的东京时间显示）。"""
    from ..db.database import parse_iso
    from zoneinfo import ZoneInfo
    dt = parse_iso(iso)
    if not dt:
        return "—"
    try:
        return dt.astimezone(ZoneInfo("Asia/Tokyo")).strftime("%m-%d %H:%M")
    except Exception:
        return iso or "—"
