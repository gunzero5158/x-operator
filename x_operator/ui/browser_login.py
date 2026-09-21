"""账号页面的可选浏览器组件、批量导入及非阻塞登录任务入口。"""
from __future__ import annotations

from .i18n import Labels, t as _tr

from nicegui import ui

from ..core import browser_login as service
from ..db.database import get_conn
from .layout import hint

STATUS = {"queued": "排队中", "running": "登录中", "manual": "等待人工验证",
          "success": "已登录", "failed": "未完成", "skipped": "已跳过", "cancelled": "已取消"}


def login_accounts(account_ids: list[int], refresh=lambda: None) -> None:
    """单账号和批量复用，未安装时仅展示下载选项，不静默安装。"""
    if not service.installed():
        browser_setup(account_ids, refresh)
        return
    try:
        service.enqueue(account_ids)
    except ValueError as exc:
        ui.notify(str(exc), type="warning")
        return
    ui.notify(_tr('已加入登录队列；可以继续使用其他功能'), type="positive")
    login_tasks(refresh)


def browser_setup(account_ids=None, refresh=lambda: None) -> None:
    with ui.dialog() as dlg, ui.card().classes("w-[560px] max-w-[95vw] gap-4"):
        ui.label(_tr('专用浏览器 · 可选组件')).classes("text-lg font-bold")
        ui.label(_tr('密码登录需要下载专用 Chromium。Cookie 登录不需要安装，下载完成后可以重复使用。'))
        hint(_tr('浏览器窗口会打开在运行服务的电脑上，需要本机有桌面环境。它不读取你日常 Chrome 的个人资料。下载使用系统代理；账号登录使用账号配置的代理。'))
        status = ui.label().classes("text-sm").props('role="status" aria-live="polite"')
        progress = ui.linear_progress(0, show_value=False).classes("w-full")

        def download():
            try:
                service.install_browser()
            except ValueError as exc:
                ui.notify(str(exc), type="warning")
            tick()

        def start():
            dlg.close()
            login_accounts(account_ids, refresh)

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button(_tr('关闭，后台继续'), on_click=dlg.close).props("flat")
            download_button = ui.button(_tr('下载 Chromium'), icon="download", on_click=download).props("outline")
            start_button = ui.button(_tr('开始登录'), icon="login", on_click=start)
            start_button.set_visibility(bool(account_ids))

        def tick():
            state = service.installation_status()
            ready, active = state["installed"], state["status"] == "downloading"
            status.set_text(_tr('专用 Chromium 已就绪') if ready and not active else state["message"])
            download_button.set_text(_tr('重新下载') if ready else _tr('重试下载') if state["status"] == "failed" else _tr('下载 Chromium'))
            download_button.set_enabled(not active)
            start_button.set_enabled(ready and not active)
            progress.set_visibility(active)
            if state["percent"] is None:
                progress.props("indeterminate")
            else:
                progress.props(remove="indeterminate")
                progress.set_value(state["percent"] / 100)

        tick()
        timer = ui.timer(1, tick)
        dlg.on("hide", timer.deactivate)
    dlg.open()


def login_tasks(refresh=lambda: None) -> None:
    with ui.dialog() as dlg, ui.card().classes("w-[780px] max-w-[95vw] max-h-[85vh] overflow-auto gap-4"):
        with ui.row().classes("w-full justify-between items-center"):
            ui.label(_tr('浏览器登录任务')).classes("text-lg font-bold")
            ui.button(_tr('关闭，后台继续'), on_click=dlg.close).props("flat")
        hint(_tr('一次处理一个账号。遇到额外验证，请在专用浏览器完成；也可以跳过当前账号，让后面的继续。任务不会因关闭此面板或切换页面而中断。服务重启后需重新发起。'))
        body = ui.column().classes("w-full gap-3")
        signature = None

        def tick():
            nonlocal signature
            jobs = service.snapshot()
            if jobs == signature:
                return
            signature = jobs
            body.clear()
            with body:
                if not jobs:
                    ui.label(_tr('暂无登录任务')).classes("text-sm")
                for job in jobs:
                    items = job["items"]
                    done = sum(i["status"] in service.TERMINAL for i in items)
                    success = sum(i["status"] == "success" for i in items)
                    with ui.card().classes("w-full gap-3"):
                        with ui.row().classes("w-full items-center justify-between"):
                            ui.label(_tr('已处理 {p0}/{p1} · 成功 {p2}', p0=done, p1=len(items), p2=success)).classes("font-semibold")
                            if not job["finished"]:
                                ui.button(_tr('取消本批'), on_click=lambda jid=job["id"]: service.cancel(jid)).props("outline color=negative")
                            else:
                                retry = [i["account_id"] for i in items if i["status"] != "success"]
                                if retry:
                                    ui.button(_tr('重试未完成账号'), icon="refresh",
                                              on_click=lambda ids=retry: login_accounts(ids, refresh)).props("outline")
                        ui.linear_progress(done / len(items), show_value=False).classes("w-full")
                        for item in items:
                            with ui.row().classes("w-full items-center gap-2"):
                                ui.label("@" + item["handle"]).classes("font-medium")
                                ui.badge(STATUS[item["status"]], color="positive" if item["status"] == "success" else
                                         "warning" if item["status"] in {"manual", "failed"} else "primary")
                                if item["status"] in {"running", "manual"}:
                                    ui.button(_tr('跳过此账号'), on_click=lambda jid=job["id"], aid=item["account_id"]: service.cancel(jid, aid)).props("flat dense")
                            ui.label(item["message"]).classes("text-sm break-words").props('role="status"')

        tick()
        timer = ui.timer(1, tick)
        dlg.on("hide", lambda: (timer.deactivate(), refresh()))
    dlg.open()


def batch_import(refresh=lambda: None) -> None:
    records: list[dict] = []
    with ui.dialog() as dlg, ui.card().classes("w-[760px] max-w-[95vw] max-h-[90vh] overflow-auto gap-3"):
        ui.label(_tr('批量导入账号')).classes("text-lg font-bold")
        ui.label(_tr('导入账号密码后，使用与单账号相同的浏览器登录流程。每批最多 200 个账号。'))
        with ui.expansion(_tr('格式与示例'), icon="help_outline").classes("w-full"):
            ui.label(_tr('每行：用户名,密码,TOTP密钥,邮箱,代理。前两列必填；未启用两步验证可留空第三列。支持 CSV、制表符或 ---- 分隔。'))
            ui.code("username,password,totp_secret,email,proxy\nexample_user,your_password,your_totp_secret,,", language="text").classes("w-full")
            ui.label(_tr('含逗号的密码请按 CSV 格式加双引号。使用账号 handle，不要把邮箱填在第一列。同名账号（含已删除账号）会跳过，不会覆盖已有设置。'))
        content = ui.textarea(_tr('粘贴账号列表')).props('outlined autogrow autocomplete="off" spellcheck="false"').classes("w-full")
        error = ui.label().classes("text-negative text-sm whitespace-pre-wrap").props('role="alert"')
        preview = ui.column().classes("w-full gap-1 max-h-48 overflow-auto")

        def reset():
            records.clear()
            preview.clear()
            submit.set_enabled(False)

        def inspect():
            reset()
            error.set_text("")
            try:
                records.extend(service.parse_import(content.value or ""))
            except ValueError as exc:
                error.set_text(str(exc))
                return
            with get_conn() as conn:
                existing = {r["handle"].lower() for r in conn.execute("SELECT handle FROM accounts")}
            with preview:
                for record in records:
                    ui.label(f"@{record['handle']} · " + (_tr('已存在，将跳过') if record["handle"].lower() in existing else _tr('将添加，等待登录')))
            submit.set_enabled(True)

        def save():
            try:
                # 提交时重新读取并校验，避免用户改了文本却沿用旧预览。
                parsed = service.parse_import(content.value or "")
                if parsed != records:
                    inspect()
                    error.set_text(_tr('内容已变化，请确认更新后的预览，再点击导入'))
                    return
                ids, skipped = service.import_accounts(parsed)
            except ValueError as exc:
                error.set_text(str(exc))
                return
            except Exception:
                error.set_text(_tr('导入未完成，数据库可能正忙，请稍后重试'))
                return
            dlg.close()
            refresh()
            ui.notify(_tr('已添加 {p0} 个账号，跳过 {p1} 个重复账号', p0=len(ids), p1=skipped), type="positive")
            if ids:
                login_accounts(ids, refresh)

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button(_tr('取消'), on_click=dlg.close).props("flat")
            ui.button(_tr('检查并预览'), icon="fact_check", on_click=inspect).props("outline")
            submit = ui.button(_tr('导入并登录'), icon="login", on_click=save)
            submit.set_enabled(False)
        content.on_value_change(lambda _: reset())
        dlg.on("hide", lambda: (records.clear(), content.set_value("")))
    dlg.open()


def toolbar(refresh) -> None:
    """常驻轻量入口；运行时才显示简短进度，不铺开账号明细。"""
    with ui.row().classes("w-full items-center gap-2"):
        ui.button(_tr('批量导入'), icon="playlist_add", on_click=lambda: batch_import(refresh)).props("outline")
        ui.button(_tr('批量登录待处理账号'), icon="login", on_click=lambda: pending_dialog(refresh)).props("outline")
        ui.button(_tr('登录任务'), icon="pending_actions", on_click=lambda: login_tasks(refresh)).props("outline")
        ui.button(_tr('浏览器组件'), icon="web", on_click=lambda: browser_setup(refresh=refresh)).props("flat")
        status = ui.label().classes("text-sm").props('role="status" aria-live="polite"')
    previous = None

    def tick():
        nonlocal previous
        jobs = service.snapshot()
        active = [i for j in jobs for i in j["items"] if i["status"] not in service.TERMINAL]
        manual = any(i["status"] == "manual" for i in active)
        status.set_text((_tr('{p0} 个账号等待完成', p0=len(active)) + (_tr(' · 需要人工验证') if manual else "")) if active else "")
        finished = sum(i["status"] in service.TERMINAL for j in jobs for i in j["items"])
        if previous is not None and previous != finished:
            refresh()
        previous = finished

    tick()
    ui.timer(1, tick)


def pending_dialog(refresh) -> None:
    rows = service.pending_accounts()
    if not rows:
        ui.notify(_tr('没有等待登录的账号；已保存 Cookie 的账号可从卡片单独重新登录'), type="info")
        return
    with ui.dialog() as dlg, ui.card().classes("w-[560px] max-w-[95vw] max-h-[85vh] overflow-auto gap-3"):
        ui.label(_tr('选择待登录账号')).classes("text-lg font-bold")
        ui.label(_tr('列出已填密码但尚未取得 Cookie，或凭据失效的账号。每批最多 200 个。'))
        selected = {}
        for row in rows:
            selected[row["id"]] = ui.checkbox("@" + row["handle"], value=len(selected) < 200)

        def start():
            ids = [aid for aid, checkbox in selected.items() if checkbox.value]
            if not 1 <= len(ids) <= 200:
                ui.notify(_tr('请选择 1–200 个账号'), type="warning")
                return
            dlg.close()
            login_accounts(ids, refresh)

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button(_tr('取消'), on_click=dlg.close).props("flat")
            ui.button(_tr('开始登录'), icon="login", on_click=start)
    dlg.open()


# Resolve display labels per client; keep core dictionaries and stored values unchanged.
STATUS = Labels(STATUS)
