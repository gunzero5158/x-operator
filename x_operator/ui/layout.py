"""公共页面外壳：导航、任务面板、内容区与页脚，以及各页共用的小工具。"""
from __future__ import annotations

from contextlib import asynccontextmanager, contextmanager
from typing import Any, Callable

from .i18n import Labels, t as _tr, localized_props

from nicegui import run, ui

from ..db.database import get_conn
from ..llm.client import timeout_for
from .disclaimer import disclaimer_footer
from .task_progress import start_task, mount_task_panel
from .theme import apply_theme
from .i18n import initialize_language, language_menu, current_language
from ..version import __version__

# (路径, 名称, material 图标)
NAV = [
    ("/", "仪表盘", "dashboard"),
    ("/queue", "任务队列", "rate_review"),
    ("/targets", "抓取记录", "travel_explore"),
    ("/materials", "素材库", "inventory_2"),
    ("/watched", "监控推主", "visibility"),
    ("/rules", "搜索规则", "manage_search"),
    ("/schedule", "定时发帖", "schedule"),
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
    initialize_language()
    apply_theme()
    with ui.header().classes("xo-header items-center justify-between"):
        # 品牌与状态一行，导航独立一行，避免挤压页面的主要操作。
        with ui.row().classes("xo-brand items-center shrink-0"):
            ui.icon("layers").classes("xo-brand-icon")
            ui.label("x-operator").classes("xo-brand-name")
            ui.label(f"v{__version__}").classes("xo-version").tooltip(_tr("当前版本"))
            ui.label(_tr('内容工作台')).classes("xo-brand-caption")

        # 保留原有导航位置和路径，当前页用短底线标识。
        pc = _pending_count()
        with ui.row().classes("xo-nav items-center").props(localized_props('role=navigation aria-label="主导航"')):
            for path, name, icon in NAV:
                is_active = path == active
                cls = "xo-nav-link" + (" is-active" if is_active else "")
                with ui.link(target=path).classes(cls).props('aria-current=page' if is_active else ''):
                    ui.icon(icon).classes("text-lg")
                    ui.label(_tr(name))
                    if path == "/queue" and pc:
                        ui.badge(str(pc), color=None).classes("xo-nav-count ml-1")

        # 右：显示时区 + 账号告警
        with ui.row().classes("xo-timezone items-center shrink-0"):
            language_menu()
            ui.icon("schedule").classes("text-base")
            ui.label(display_tz_name()) \
                .tooltip(_tr('页面上所有时间按这个时区显示，可在「设置 → 自动运行」里改；不影响账号活跃时段 / 定时计划的判断'))
            ac = _alert_count()
            if ac:
                ui.badge(_tr('⚠ {p0} 账号凭据失效', p0=ac), color=None).classes("bg-red-600 text-white")
    mount_task_panel()
    container = ui.column().classes("xo-page")
    with container:
        yield container
        disclaimer_footer()


# ------------------------------------------------------------------------------------
# 各页共用的小工具
# ------------------------------------------------------------------------------------
async def confirm(title: str, detail: str = "", ok_label: str = _tr('确认删除'), color: str = "negative") -> bool:
    """弹确认框，返回用户是否点了确认。"""
    with ui.dialog() as dlg, ui.card().classes("min-w-80"):
        ui.label(title).classes("text-lg font-bold")
        if detail:
            ui.label(detail).classes("text-sm text-gray-500")
        with ui.row().classes("w-full justify-end gap-2"):
            ui.button(_tr('取消'), on_click=lambda: dlg.submit(False)).props("flat")
            ui.button(_tr(ok_label), on_click=lambda: dlg.submit(True)).props(f"color={color}")
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
        ui.notify(_tr('{p0}出错：{p1}', p0=label, p1=e), type="negative", multi_line=True, close_button=True, timeout=12000)
        if refresh:
            refresh()
        return None
    ok = getattr(res, "ok", True)
    msg = res.as_msg() if hasattr(res, "as_msg") else _tr('{p0}完成', p0=label)
    if result_link:
        text, href = result_link
        with ui.dialog() as dlg, ui.card().classes("min-w-96 max-w-[90vw]"):
            with ui.row().classes("items-center gap-2"):
                ui.icon("check_circle" if ok else "warning").classes("text-2xl " + ("text-green-600" if ok else "text-orange-500"))
                ui.label(_tr('{p0}结果', p0=label)).classes("text-lg font-bold")
            ui.label(msg).classes("text-sm whitespace-pre-wrap")
            with ui.row().classes("w-full justify-end gap-2"):
                ui.button(_tr('关闭'), on_click=dlg.close).props("flat")
                ui.button(text, icon="arrow_forward", on_click=lambda: ui.navigate.to(href)).props("color=primary")
        dlg.open()
    else:
        notify_long(msg, ok=ok)
    if refresh:
        refresh()
    return res


@asynccontextmanager
async def llm_wait(label: str, scene: str = "write", note: str = "", result_link: tuple[str, str] | None = None):
    """AI 等待使用不阻挡操作的任务面板；不把超时比例冒充真实生成进度。"""
    client = ui.context.client
    task = start_task(label, result_link=result_link,
                      note=note or _tr('AI 正在生成，单次请求超时 {p0} 秒；重写可能需要更久。', p0=timeout_for(scene)))
    task.message = _tr('AI 正在生成…')
    try:
        # 触发按钮所在的卡片可能被其他操作刷新，后续提示挂在页面根节点。
        with client.content:
            yield task
    except Exception as e:
        task.finish(_tr('{p0}出错：{p1}', p0=label, p1=e), ok=False)
        raise
    finally:
        if task.finished is None:
            task.finish(_tr('{p0}处理完成', p0=label))


async def run_job_with_progress(fn: Callable[..., Any], label: str, refresh: Callable[[], None] | None = None,
                                result_link: tuple[str, str] | None = None, *, task_key: str | None = None):
    """在线程池执行任务，进度保存在进程中，页面刷新/切换不影响执行。"""
    client = ui.context.client
    task = start_task(label, key=task_key or label, result_link=result_link)
    if task is None:
        with client.content:
            ui.notify(_tr('这个任务正在运行，请在右下角查看进度'), type="info")
        return None
    try:
        res = await run.io_bound(fn, task.update)
        ok = getattr(res, "ok", True)
        msg = res.as_msg() if hasattr(res, "as_msg") else _tr('{p0}完成', p0=label)
    except Exception as e:
        res, ok, msg = None, False, _tr('{p0}出错：{p1}', p0=label, p1=e)
    task.finish(msg, ok=ok)
    if refresh and not client.is_deleted:
        with client.content:
            refresh()
    return res


def tweet_link(author_handle: str | None, tweet_id: str | None):
    """指向 X 上原推的链接。"""
    if not tweet_id:
        return
    if not str(tweet_id).isdigit():
        ui.label(_tr('推文 id {p0}（非真实链接）', p0=tweet_id)).classes("text-xs text-gray-400")
    else:
        ui.link(_tr('在 X 上打开原推 ↗'), f"https://x.com/{author_handle or 'i'}/status/{tweet_id}",
                new_tab=True).classes("text-xs")


TARGET_STATUS_LABEL = {
    "new": "待匹配",
    "queued": "已进任务队列",
    "no_match": "达标但未生成回复",
    "filtered": "已过滤/未达标",
    "expired": "回复草稿已过期",
}

QUEUE_STATUS_LABEL = {
    "pending": "待审核", "approved": "待发送", "sending": "发送中", "sent": "已发送",
    "failed": "失败", "skipped": "已跳过", "expired": "已过期",
}


# 标签配色约定（全站统一）：颜色 = 这个标签在说哪一类信息，各页面卡片都按这套来
TAG = {
    "source": "xo-tag xo-tag-source",
    "account": "xo-tag xo-tag-account",
    "reply": "xo-tag xo-tag-reply",
    "post": "xo-tag xo-tag-post",
    "ok": "xo-tag xo-tag-ok",
    "wait": "xo-tag xo-tag-wait",
    "attn": "xo-tag xo-tag-attn",
    "bad": "xo-tag xo-tag-bad",
    "off": "xo-tag xo-tag-off",
    "metric": "xo-tag xo-tag-metric",
    "metric_ok": "xo-tag xo-tag-metric_ok",
    "metric_bad": "xo-tag xo-tag-metric_bad",
    "mode": "xo-tag xo-tag-mode",
    "ai": "xo-tag xo-tag-ai",
    "warn": "xo-tag xo-tag-warn",
    "media": "xo-tag xo-tag-media",
}

TAG_LEGEND = [("来源", "source"), ("账号", "account"), ("回复", "reply"), ("发帖", "post"), ("正常", "ok"), ("等待", "wait"),
              ("待处理", "attn"), ("失败", "bad"), ("停用", "off"), ("数值 / 参数", "metric"), ("方式", "mode"), ("AI", "ai"),
              ("注意", "warn"), ("附件", "media")]


def tag(text: str, kind: str = "metric", tooltip: str | None = None):
    """统一样式的小标签。kind 见 TAG。"""
    b = ui.badge(text, color=None).classes(TAG.get(kind, TAG["metric"]))
    if tooltip:
        b.tooltip(tooltip)
    return b


_FEED_KIND_WORD = {"feed_for_you": "推荐流", "feed_following": "关注流"}


def source_label(source: str | None, rule_name: str | None, rule_kind: str | None, watched_handle: str | None) -> str:
    """抓取记录 / 任务队列共用：这条推文是哪条搜索规则 / 哪个监控推主抓来的。
    例：搜索「日本开发者」、推荐流「养号 A」、监控 @someone；规则或推主被删了也给个说法。"""
    if source == "monitor":
        return _tr('监控 @{p0}', p0=watched_handle) if watched_handle else _tr('监控（推主已删）')
    if rule_name:
        return f"{_FEED_KIND_WORD.get(rule_kind or '', _tr('搜索'))}「{rule_name}」"
    return _tr('搜索（规则已删）')


HINT_CLAMP_CHARS = 60   # 说明超过这么多字就折成一行，点「更多」展开


def hint(text: str, after_row: bool = False, *, clamp: bool = True):
    """独立说明区；长说明可点击或用键盘展开。after_row 保留调用兼容性。"""
    text = _tr(text)
    margin = "mb-1"
    if not clamp or len(text) <= HINT_CLAMP_CHARS:
        return ui.label(text).classes("xo-hint-short text-xs text-gray-400 " + margin)
    with ui.row(wrap=False).classes("xo-hint " + margin).props(localized_props('role=button tabindex=0 aria-expanded=false aria-label="展开说明"')) as row:
        ui.icon("help_outline").classes("xo-hint__icon")
        ui.label(_tr('说明')).classes("xo-hint__label")
        ui.label(text).classes("xo-hint__text text-xs text-gray-400")
        more = ui.label(_tr('更多')).classes("xo-hint__more")

    def toggle(_=None):
        opening = "is-open" not in row._classes
        row.classes(add="is-open" if opening else "", remove="" if opening else "is-open")
        more.set_text(_tr('收起') if opening else _tr('更多'))
        accessible_label = _tr('收起说明') if opening else _tr('展开说明')
        row.props(f'aria-expanded={str(opening).lower()} aria-label="{accessible_label}"')
    row.on("click", toggle)
    row.on("keydown.enter", toggle)
    row.on("keydown.space.prevent", toggle)
    return row


def tag_legend(kinds: list[str] | None = None):
    """标签图例按需展开，避免每页都重复铺开。"""
    items = [(t, k) for t, k in TAG_LEGEND if kinds is None or k in kinds]
    with ui.expansion(_tr('标签颜色说明'), icon="info_outline").classes("xo-help w-full") as row:
        with ui.row().classes("xo-tag-legend items-center flex-wrap"):
            for text, kind in items:
                ui.badge(_tr(text), color=None).classes(TAG[kind])
    return row


def page_title(title: str, subtitle: str):
    """统一页标题；返回标题 label，兼容素材库切换回收站。"""
    with ui.column().classes("xo-page-title"):
        label = ui.label(title).classes("text-2xl font-bold")
        ui.label(subtitle).classes("xo-page-subtitle")
    return label


def detail_text(title: str, text: str, *, warning: bool = False):
    """完整详情按需展开；问题摘要始终可见，不隐藏失败状态。"""
    summary = " ".join((text or "").split())
    summary = summary[:64] + ("…" if len(summary) > 64 else "")
    with ui.expansion(f"{title} · {summary}", icon="info_outline" if warning else "notes").classes(
            "xo-detail w-full" + (" xo-detail-warning" if warning else "")) as detail:
        ui.label(text).classes("xo-prose whitespace-pre-wrap")
    return detail


def preview_text(text: str):
    """长正文先展示四行，可随时展开原文；短正文不增加按钮。"""
    text = text or ""
    with ui.column().classes("xo-preview w-full gap-1") as box:
        label = ui.label(text).classes("xo-prose whitespace-pre-wrap")
        if len(text) > 180 or text.count("\n") > 3:
            label.classes("xo-preview-clamped")

            def toggle():
                opening = "xo-preview-clamped" in label._classes
                label.classes(remove="xo-preview-clamped" if opening else "",
                              add="" if opening else "xo-preview-clamped")
                button.set_text(_tr('收起正文') if opening else _tr('展开全文'))
                button.props(f'aria-expanded={str(opening).lower()}')

            button = ui.button(_tr('展开全文'), on_click=toggle).props("flat dense aria-expanded=false").classes("xo-read-more")
    return box


def fmt_views(n: int | None) -> str:
    """观看量的易读写法：1234 → 1234；12345 → 1.2万；1234567 → 123.5万。"""
    if n is None:
        return "—"
    if current_language() == "en":
        for size, unit in ((1_000_000, "M"), (1_000, "K")):
            if n >= size:
                return f"{n / size:.1f}".removesuffix(".0") + unit
        return str(n)
    if n >= 10000:
        return _tr('{p0}万', p0=format(n / 10000, '.1f')).replace(".0万", "万")
    return str(n)


DEFAULT_DISPLAY_TZ = "Asia/Tokyo"
_display_tz_cache: dict = {"name": None, "zone": None}


def display_tz_name() -> str:
    """设置 → 自动运行 里选的「界面显示时区」。"""
    from .. import config
    return (config.get("display_timezone") or DEFAULT_DISPLAY_TZ).strip() or DEFAULT_DISPLAY_TZ


def display_tz():
    """显示用的 ZoneInfo。列表页每行都要转一次，按名字缓存，设置页改完调 refresh_display_tz()。"""
    from zoneinfo import ZoneInfo
    if _display_tz_cache["zone"] is None:
        name = display_tz_name()
        try:
            zone = ZoneInfo(name)
        except Exception:
            zone = ZoneInfo(DEFAULT_DISPLAY_TZ)
        _display_tz_cache.update(name=name, zone=zone)
    return _display_tz_cache["zone"]


def refresh_display_tz() -> None:
    _display_tz_cache.update(name=None, zone=None)


def fmt_time(iso: str | None) -> str:
    """UTC ISO → 易读（浏览器所在时区不可知，按设置里的「界面显示时区」显示，默认东京）。"""
    from ..db.database import parse_iso
    dt = parse_iso(iso)
    if not dt:
        return "—"
    try:
        return dt.astimezone(display_tz()).strftime("%m-%d %H:%M")
    except Exception:
        return iso or "—"


# Resolve display labels per client; keep core dictionaries and stored values unchanged.
TARGET_STATUS_LABEL = Labels(TARGET_STATUS_LABEL)
QUEUE_STATUS_LABEL = Labels(QUEUE_STATUS_LABEL)
_FEED_KIND_WORD = Labels(_FEED_KIND_WORD)
