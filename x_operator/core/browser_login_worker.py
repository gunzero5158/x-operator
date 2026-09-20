"""隔离的有窗口登录进程。凭据只经 stdin/stdout 管道传递，不写命令行或日志。

只操作 X 登录表单；验证码/未知页面交给用户，不尝试绕过人机验证。
此进程不访问数据库，账号核对与保存由父进程完成。
"""
from __future__ import annotations

import json
import re
import sys
import time
from urllib.parse import unquote, urlsplit


def emit(kind: str, **fields) -> None:
    print(json.dumps({"kind": kind, **fields}, ensure_ascii=True), flush=True)


def proxy_options(raw: str | None) -> dict:
    if not raw:
        return {}
    parts = urlsplit(raw)
    if parts.scheme not in {"http", "https", "socks5"} or not parts.hostname:
        raise ValueError("代理格式不支持，请使用 http、https 或 socks5 地址")
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    opts = {"server": f"{parts.scheme}://{host}" + (f":{parts.port}" if parts.port else "")}
    if parts.username:
        opts.update(username=unquote(parts.username), password=unquote(parts.password or ""))
    return {"proxy": opts}


def login(request: dict) -> None:
    import pyotp
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

    creds = request["credentials"]
    expected = request["handle"].lower().lstrip("@")
    options = proxy_options(request.get("proxy"))
    if request.get("direct"):
        options["args"] = ["--no-proxy-server"]
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False, **options)
        try:
            # 每次创建全新上下文，既不读取日常 Chrome 资料，也不串用其他账号 Cookie。
            context = browser.new_context(locale="en-US", accept_downloads=False)
            page = context.new_page()
            page.set_default_timeout(1500)
            emit("progress", message="正在打开 X 登录页面")
            try:
                page.goto("https://x.com/i/flow/login", wait_until="domcontentloaded", timeout=60000)
            except PlaywrightTimeout:
                pass  # 页面可能已经可用，继续检查可见登录表单。
            deadline = time.monotonic() + 600
            last_action = time.monotonic()
            submitted: set[str] = set()
            manual = False

            def handoff(message: str) -> None:
                nonlocal manual
                manual = True
                emit("manual", message=message + "；请在弹出的浏览器中完成登录，或跳过此账号。最多等待 10 分钟")

            def first_visible(locator):
                for index in range(min(locator.count(), 10)):
                    item = locator.nth(index)
                    if item.is_visible() and item.is_enabled():
                        return item
                return None

            def enter(field, value: str, step: str, message: str) -> None:
                nonlocal last_action
                if step in submitted:
                    return
                # 切换阶段时再次核对目标域名，避免把凭据填入非 X 页面。
                target = urlsplit(page.url)
                if target.scheme != "https" or target.hostname != "x.com":
                    handoff("页面离开 X 登录域名，已停止自动输入")
                    return
                submitted.add(step)
                field.fill(value)
                emit("progress", message=message)
                # 新版验证码填满即自动提交；只在原字段仍可编辑时按回车。
                page.wait_for_timeout(500)
                if field.is_visible() and field.is_enabled():
                    button = first_visible(page.get_by_role("button", name=re.compile(
                        r"^(Continue|Next|Log in|Verify|继续|下一步|登录|验证|次へ|ログイン)$", re.I)))
                    if button:
                        button.click()
                    else:
                        field.press("Enter")
                last_action = time.monotonic()

            while time.monotonic() < deadline:
                if page.is_closed():
                    emit("error", message="登录窗口已关闭，可以重新发起登录")
                    return
                location = urlsplit(page.url)
                if location.scheme != "https" or location.hostname != "x.com":
                    if not manual:
                        handoff("当前不是 X 登录页面，已停止自动输入")
                    page.wait_for_timeout(750)
                    continue
                cookies = {c["name"]: c["value"] for c in context.cookies("https://x.com")
                           if c["name"] in {"auth_token", "ct0"}}
                if cookies.get("auth_token") and cookies.get("ct0"):
                    profile = page.locator('a[data-testid="AppTabBar_Profile_Link"]')
                    if not profile.count():
                        profile = page.get_by_role("link", name=re.compile(r"^(Profile|个人资料|プロフィール)$"))
                    if profile.count():
                        href = profile.first.get_attribute("href") or ""
                        actual = urlsplit(href).path.strip("/").lower()
                        if actual != expected:
                            emit("error", message="登录后的账号与配置的 handle 不一致，未保存 Cookie，请核对账号")
                            return
                        emit("success", handle=actual, cookies=cookies)
                        return
                if manual:
                    page.wait_for_timeout(750)
                    continue
                try:
                    content = page.locator("body").inner_text(timeout=1500)
                    challenge = page.locator('iframe[src*="arkoselabs"], iframe[src*="captcha"]')
                    if challenge.count() or re.search(r"authenticate your account|verify you are human|人机验证|验证你是真人", content, re.I):
                        handoff("X 要求人机验证，自动操作已暂停")
                    elif re.search(r"incorrect password|wrong password|密码错误|密码不正确|wrong.*code|incorrect.*code|验证码.*(错误|不正确)", content, re.I):
                        handoff("X 未接受密码或验证码，自动重试已停止")
                    else:
                        password = first_visible(page.locator('input[type="password"]'))
                        code = first_visible(page.get_by_role("textbox", name=re.compile(r"verification code|authentication code|验证码|認証コード", re.I)))
                        if code is None:
                            code = first_visible(page.locator('input[autocomplete="one-time-code"]'))
                        username = first_visible(page.get_by_role("textbox", name=re.compile(r"email or username|phone, email|电子邮箱或用户名|手机.*邮箱|電話番号.*ユーザー", re.I)))
                        if username is None:
                            username = first_visible(page.locator('input[autocomplete="username"]'))
                        # 邮箱/短信验证码不能用 TOTP；只在明确的验证器页面自动填写。
                        totp_screen = bool(re.search(r"authenticator|authentication app|验证器|身份验证应用|認証アプリ", content, re.I))
                        if password is not None:
                            enter(password, creds["password"], "password", "已提交密码，等待 X 验证")
                        elif code is not None and totp_screen:
                            if not creds.get("totp_secret"):
                                handoff("账号启用了两步验证，但未填写 TOTP 密钥")
                            elif "totp" not in submitted:
                                # 避免刚填完就到期；仅提交一次，失败由用户接手。
                                if time.time() % 30 > 24:
                                    page.wait_for_timeout(750)
                                    continue
                                value = pyotp.TOTP(creds["totp_secret"]).now()
                                enter(code, value, "totp", "已提交两步验证码，等待登录完成")
                        elif code is not None:
                            handoff("X 要求额外验证码，请在浏览器中填写")
                        elif username is not None:
                            enter(username, creds["username"], "username", "已提交账号，等待密码页面")
                        elif time.monotonic() - last_action > 30:
                            handoff("X 显示了额外验证或未识别的登录步骤")
                        if time.monotonic() - last_action > 45 and not manual:
                            handoff("登录步骤暂未完成，请检查浏览器中的提示")
                except PlaywrightTimeout:
                    if time.monotonic() - last_action > 45 and not manual:
                        handoff("页面响应较慢，请在浏览器中检查登录状态")
                page.wait_for_timeout(750)
            emit("error", message="登录等待超过 10 分钟，已关闭窗口；可在任务列表重新登录")
        finally:
            browser.close()


def main() -> None:
    try:
        request = json.loads(sys.stdin.readline())
        login(request)
    except Exception:
        # Playwright 的异常可能带输入值/代理/页面文本，不把原始异常传回界面或日志。
        emit("error", message="浏览器登录未完成：请检查 Chromium 安装、代理和桌面环境后重试；也可使用 Cookie 登录")


if __name__ == "__main__":
    main()
