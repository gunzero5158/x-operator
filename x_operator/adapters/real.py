"""真实适配器：official=tweepy（X API v2）/ unofficial=twifork（twikit 分支，Cookie 登录）。

凭据来自 accounts.credentials（JSON，设置页「账号」里填）：
- official  : consumer_key / consumer_secret / access_token / access_token_secret（必填 4 项）
              bearer_token（选填，只读接口优先用它省用户额度）
- unofficial: auth_token + ct0（浏览器 Cookie，推荐）；或 username(+email)/password(+totp_secret 两步验证密钥)
              走自研登录流程，成功后自动把 Cookie 写回账号，Cookie 失效时自动重新登录。
              proxy 选填：留空自动用系统当前代理，填 direct 强制直连。

所有异常统一转换为 base.py 的 XClientError 家族，message 为中文人话；
只有 RateLimited / NetworkError 可重试（dispatcher 依此决定重试）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .base import (AuthExpired, CredentialMissing, DuplicateContent, FetchResult,
                   MediaError, NetworkError, PermissionDenied, PostResult, RateLimited,
                   TargetNotFound, TweetData, UserData, XClient, XClientError)

log = logging.getLogger("x_operator.adapters")

OFFICIAL_REQUIRED = ("consumer_key", "consumer_secret", "access_token", "access_token_secret")


def parse_credentials(raw: str | None) -> dict:
    try:
        obj = json.loads(raw or "{}")
        return obj if isinstance(obj, dict) else {}
    except (TypeError, ValueError):
        return {}


def credentials_ready(access_type: str, creds: dict) -> tuple[bool, str]:
    """(是否齐全, 缺什么的中文说明)。"""
    if access_type == "official":
        missing = [k for k in OFFICIAL_REQUIRED if not (creds.get(k) or "").strip()]
        if missing:
            return False, "缺少：" + "、".join(missing)
        return True, "官方 API 凭据已填"
    has_cookie = bool((creds.get("auth_token") or "").strip() and (creds.get("ct0") or "").strip())
    has_login = bool((creds.get("username") or "").strip() and (creds.get("password") or "").strip())
    if has_cookie:
        return True, "Cookie 已填（auth_token + ct0）"
    if has_login:
        return True, "已填账号密码（首次连接时登录并保存 Cookie）"
    return False, "缺少 Cookie（auth_token + ct0）或 账号/密码"


_HEX = set("0123456789abcdef")


def normalize_totp_secret(raw: str | None) -> str:
    """去掉空格/横线、转大写；返回空串表示没填。"""
    return "".join((raw or "").split()).replace("-", "").upper()


def validate_unofficial_credentials(creds: dict) -> str:
    """保存前的格式体检，返回中文问题描述（空串 = 没问题）。只查格式，不联网。"""
    at = (creds.get("auth_token") or "").strip()
    ct0 = (creds.get("ct0") or "").strip()
    if at or ct0:
        if not at or not ct0:
            return "auth_token 和 ct0 要一起填（两个都在浏览器同一个 Cookie 列表里）"
        if len(at) != 40 or not set(at.lower()) <= _HEX:
            return (f"auth_token 格式不对：应该是 40 位、只含 0-9 a-f 的字符串，你填的长度是 {len(at)}。"
                    "请确认复制的是「Value / 值」那一列，并且没有多复制空格或别的字段")
        if len(ct0) < 32 or not set(ct0.lower()) <= _HEX:
            return (f"ct0 格式不对：应该是 32 位或 160 位、只含 0-9 a-f 的字符串，你填的长度是 {len(ct0)}。"
                    "请确认复制的是「Value / 值」那一列")
    totp = normalize_totp_secret(creds.get("totp_secret"))
    if totp:
        if totp.isdigit() and len(totp) <= 8:
            return "TOTP 密钥填错了：那是验证器 App 里每 30 秒变一次的 6 位数字。这里要填的是绑定两步验证时 X 给的「密钥」（一长串字母数字，如 JBSWY3DPEHPK3PXP）"
        if not set(totp) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567="):
            return "TOTP 密钥格式不对：只能包含字母 A-Z 和数字 2-7（Base32），请重新核对"
    return ""


def detect_system_proxy() -> str | None:
    """系统当前代理。Windows 读「设置 → 网络和 Internet → 代理」（注册表）；其他平台读
    HTTPS_PROXY / HTTP_PROXY / ALL_PROXY 环境变量。没有代理返回 None。

    注意 Windows 注册表里同一个代理会被 Python 拼成 https://host:port，那样会误以为要和代理
    做 TLS 握手而连不上；这里统一改成 http:// 前缀。"""
    try:
        import urllib.request
        proxies = urllib.request.getproxies()
    except Exception:
        return None
    for key in ("http", "https", "all"):
        v = (proxies.get(key) or "").strip()
        if not v:
            continue
        if v.startswith("https://"):
            v = "http://" + v[len("https://"):]
        elif "://" not in v:
            v = "http://" + v
        return v
    return None


def resolve_proxy(creds: dict) -> str | None:
    """账号里填了代理就用填的；填 direct 强制直连；留空则自动用系统当前代理。"""
    raw = (creds.get("proxy") or "").strip()
    if raw.lower() in ("direct", "none", "no", "直连"):
        return None
    if raw:
        return raw if "://" in raw else "http://" + raw
    return detect_system_proxy()


def describe_proxy(proxy: str | None) -> str:
    return f"经代理 {proxy}" if proxy else "直连（未检测到系统代理）"


# ====================================================================================
# 官方 API（tweepy）
# ====================================================================================
class OfficialXClient(XClient):
    api_kind = "x_official"

    def __init__(self, credentials: dict | None = None, **_ignored):
        self.creds = credentials or {}
        ok, why = credentials_ready("official", self.creds)
        if not ok:
            raise CredentialMissing(f"官方 API 凭据不完整（{why}）。请到「设置 → 账号 → 编辑」填写 X 开发者平台的 4 项密钥。")
        try:
            import tweepy  # noqa: F401
        except ImportError as e:
            raise CredentialMissing("缺少 tweepy 库：请在项目目录运行 `uv sync` 后重启。") from e
        self._tweepy = tweepy
        self._client = tweepy.Client(
            bearer_token=(self.creds.get("bearer_token") or None),
            consumer_key=self.creds["consumer_key"].strip(),
            consumer_secret=self.creds["consumer_secret"].strip(),
            access_token=self.creds["access_token"].strip(),
            access_token_secret=self.creds["access_token_secret"].strip(),
            wait_on_rate_limit=False,
        )
        self.proxy_used = resolve_proxy(self.creds)
        if self.proxy_used:
            self._client.session.proxies = {"http": self.proxy_used, "https": self.proxy_used}
        self._me: UserData | None = None

    # ---- 异常映射 ----
    def _wrap(self, e: Exception, what: str) -> XClientError:
        t = self._tweepy
        msg = _short(str(e))
        if isinstance(e, t.TooManyRequests):
            reset = None
            try:
                hdr = e.response.headers.get("x-rate-limit-reset")
                if hdr:
                    reset = datetime.fromtimestamp(int(hdr), tz=timezone.utc)
            except Exception:
                pass
            return RateLimited(f"X API 限流（{what}），稍后自动重试。{msg}", reset_at=reset, raw=e)
        if isinstance(e, t.Unauthorized):
            return AuthExpired(f"X API 鉴权失败（{what}）：密钥无效或已被撤销，请重新填写凭据。{msg}", raw=e)
        if isinstance(e, t.Forbidden):
            low = str(e).lower()
            if "duplicate" in low:
                return DuplicateContent(f"X 判定为重复内容，拒绝发送（{what}）。", raw=e)
            return PermissionDenied(f"X API 拒绝操作（{what}）：可能是应用权限不足（需 Read and Write）、"
                                    f"对方限制回复或账号被限写。{msg}", raw=e)
        if isinstance(e, t.NotFound):
            return TargetNotFound(f"目标不存在或已删除（{what}）。{msg}", raw=e)
        if isinstance(e, t.TwitterServerError):
            return NetworkError(f"X 服务端错误（{what}），稍后重试。{msg}", raw=e)
        if isinstance(e, t.BadRequest):
            return XClientError(f"X API 请求参数错误（{what}）：{msg}", raw=e)
        if isinstance(e, t.TweepyException):
            return NetworkError(f"网络/请求异常（{what}）：{msg}", raw=e)
        return XClientError(f"未知错误（{what}）：{msg}", raw=e)

    # ---- 只读 ----
    def get_me(self) -> UserData:
        if self._me:
            return self._me
        try:
            resp = self._client.get_me(user_auth=True)
        except Exception as e:
            raise self._wrap(e, "获取本账号信息")
        d = resp.data
        self._me = UserData(user_id=str(d.id), handle=d.username, display_name=d.name or "")
        return self._me

    def get_user_by_handle(self, handle: str) -> UserData:
        h = handle.lstrip("@").strip()
        try:
            resp = self._client.get_user(username=h, user_auth=True)
        except Exception as e:
            raise self._wrap(e, f"查询用户 @{h}")
        if not resp.data:
            raise TargetNotFound(f"找不到用户 @{h}")
        d = resp.data
        return UserData(user_id=str(d.id), handle=d.username, display_name=d.name or "")

    def tweet_exists(self, tweet_id: str) -> bool | None:
        try:
            resp = self._client.get_tweet(tweet_id, user_auth=True)
        except self._tweepy.NotFound:
            return False
        except Exception:
            return None
        return bool(resp and resp.data)

    _TWEET_FIELDS = ["created_at", "lang", "referenced_tweets", "author_id", "in_reply_to_user_id"]

    def _to_tweets(self, resp) -> list[TweetData]:
        users = {}
        try:
            for u in (resp.includes or {}).get("users", []):
                users[str(u.id)] = u.username
        except Exception:
            pass
        out: list[TweetData] = []
        for tw in (resp.data or []):
            refs = tw.referenced_tweets or []
            is_rt = any(getattr(r, "type", None) == "retweeted" for r in refs)
            reply_to = next((str(r.id) for r in refs if getattr(r, "type", None) == "replied_to"), None)
            created = tw.created_at or datetime.now(timezone.utc)
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            out.append(TweetData(
                tweet_id=str(tw.id), author_id=str(tw.author_id),
                author_handle=users.get(str(tw.author_id), ""),
                text=tw.text or "", lang=tw.lang, created_at=created,
                is_retweet=is_rt, in_reply_to_tweet_id=reply_to,
            ))
        out.sort(key=lambda t: int(t.tweet_id))
        return out

    def get_user_tweets(self, user_id: str, since_id: str | None = None,
                        max_results: int = 5, include_replies: bool = False) -> FetchResult:
        exclude = ["retweets"] + ([] if include_replies else ["replies"])
        params: dict[str, Any] = dict(
            max_results=max(5, min(100, max_results)), exclude=exclude,
            tweet_fields=self._TWEET_FIELDS, expansions=["author_id"], user_fields=["username"],
            user_auth=True,
        )
        if since_id:
            params["since_id"] = since_id
        try:
            resp = self._client.get_users_tweets(user_id, **params)
        except Exception as e:
            raise self._wrap(e, "拉取推主时间线")
        tweets = self._to_tweets(resp)
        newest = tweets[-1].tweet_id if tweets else None
        return FetchResult(tweets=tweets, newest_id=newest, reads_consumed=len(tweets))

    def search_recent(self, query: str, since_id: str | None = None,
                      start_time: datetime | None = None, max_results: int = 15) -> FetchResult:
        params: dict[str, Any] = dict(
            max_results=max(10, min(100, max_results)),
            tweet_fields=self._TWEET_FIELDS, expansions=["author_id"], user_fields=["username"],
            user_auth=True,
        )
        if since_id:
            params["since_id"] = since_id
        elif start_time:
            params["start_time"] = start_time
        try:
            resp = self._client.search_recent_tweets(query, **params)
        except self._tweepy.BadRequest as e:
            # since_id 超过 7 天会被 X 拒绝：退化为按时间窗口重查
            if since_id and "since_id" in str(e):
                params.pop("since_id", None)
                params["start_time"] = datetime.now(timezone.utc) - timedelta(hours=48)
                try:
                    resp = self._client.search_recent_tweets(query, **params)
                except Exception as e2:
                    raise self._wrap(e2, "搜索推文")
            else:
                raise self._wrap(e, "搜索推文")
        except Exception as e:
            raise self._wrap(e, "搜索推文")
        tweets = self._to_tweets(resp)
        newest = tweets[-1].tweet_id if tweets else None
        return FetchResult(tweets=tweets, newest_id=newest, reads_consumed=len(tweets))

    # ---- 写 ----
    def post(self, text: str, media_ids: list[str] | None = None) -> PostResult:
        try:
            resp = self._client.create_tweet(text=text, media_ids=media_ids or None, user_auth=True)
        except Exception as e:
            raise self._wrap(e, "发推")
        return PostResult(tweet_id=str(resp.data["id"]))

    def reply(self, text: str, in_reply_to_tweet_id: str,
              media_ids: list[str] | None = None) -> PostResult:
        try:
            resp = self._client.create_tweet(text=text, in_reply_to_tweet_id=in_reply_to_tweet_id,
                                             media_ids=media_ids or None, user_auth=True)
        except Exception as e:
            raise self._wrap(e, "回复推文")
        return PostResult(tweet_id=str(resp.data["id"]))

    def upload_media(self, file_path: str, media_type: str, alt_text: str | None = None) -> str:
        t = self._tweepy
        try:
            auth = t.OAuth1UserHandler(self.creds["consumer_key"].strip(), self.creds["consumer_secret"].strip(),
                                       self.creds["access_token"].strip(), self.creds["access_token_secret"].strip())
            api = t.API(auth, proxy=self.proxy_used) if self.proxy_used else t.API(auth)
            media = api.media_upload(file_path)
            if alt_text:
                try:
                    api.create_media_metadata(media.media_id_string, alt_text)
                except Exception:
                    pass
            return media.media_id_string
        except Exception as e:
            raise MediaError(f"媒体上传失败：{_short(str(e))}", raw=e)


# ====================================================================================
# 非官方（twifork / twikit，Cookie 登录）
# ====================================================================================
# 登录流程第一步的初始化参数（与 X 网页端一致；照搬 twikit 的版本表）
_LOGIN_FLOW_INIT = {
    "input_flow_data": {"flow_context": {"debug_overrides": {}, "start_location": {"location": "splash_screen"}}},
    "subtask_versions": {
        "action_list": 2, "alert_dialog": 1, "app_download_cta": 1, "check_logged_in_account": 1,
        "choice_selection": 3, "contacts_live_sync_permission_prompt": 0, "cta": 7, "email_verification": 2,
        "end_flow": 1, "enter_date": 1, "enter_email": 2, "enter_password": 5, "enter_phone": 2,
        "enter_recaptcha": 1, "enter_text": 5, "enter_username": 2, "generic_urt": 3, "in_app_notification": 1,
        "interest_picker": 3, "js_instrumentation": 1, "menu_dialog": 1, "notifications_permission_prompt": 2,
        "open_account": 2, "open_home_timeline": 1, "open_link": 1, "phone_verification": 4, "privacy_options": 1,
        "security_key": 3, "select_avatar": 4, "select_banner": 2, "settings_list": 7, "show_code": 1,
        "sign_up": 2, "sign_up_review": 4, "tweet_selection_urt": 1, "update_users": 1, "upload_media": 1,
        "user_recommendations_list": 4, "user_recommendations_urt": 1, "wait_spinner": 3, "web_modal": 1,
    },
}


class _LoopThread:
    """独立线程跑一个常驻 asyncio 事件循环：twikit 全 async，而调用方（NiceGUI 线程池 /
    APScheduler 线程）是同步代码，且主线程本身已有事件循环，不能直接 asyncio.run。"""

    _instance: "_LoopThread | None" = None
    _lock = threading.Lock()

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, name="twikit-loop", daemon=True)
        self.thread.start()

    @classmethod
    def get(cls) -> "_LoopThread":
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def run(self, coro, timeout: float = 90.0):
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return fut.result(timeout=timeout)


class UnofficialXClient(XClient):
    api_kind = "x_unofficial"

    def __init__(self, credentials: dict | None = None,
                 on_cookies_refreshed: Callable[[dict], None] | None = None, **_ignored):
        self.creds = credentials or {}
        ok, why = credentials_ready("unofficial", self.creds)
        if not ok:
            raise CredentialMissing(f"非官方通道凭据不完整（{why}）。请到「设置 → 账号 → 编辑」填写浏览器 Cookie。")
        try:
            import twikit
            import twikit.errors as terr
        except ImportError as e:
            raise CredentialMissing("缺少 twifork 库：请在项目目录运行 `uv sync` 后重启。") from e
        self._twikit = twikit
        self._err = terr
        self._on_cookies = on_cookies_refreshed
        self._loop = _LoopThread.get()
        self.proxy_used = resolve_proxy(self.creds)
        self._client = twikit.Client(language="ja", proxy=self.proxy_used)
        self._logged_in = False
        self._relogin_tried = False
        self._me: UserData | None = None

    # ---- 登录 ----
    def _has_password_login(self) -> bool:
        return bool((self.creds.get("username") or "").strip() and self.creds.get("password"))

    def _ensure_login(self) -> None:
        if self._logged_in:
            return
        auth_token = (self.creds.get("auth_token") or "").strip()
        ct0 = (self.creds.get("ct0") or "").strip()
        if auth_token and ct0:
            self._client.set_cookies({"auth_token": auth_token, "ct0": ct0}, clear_cookies=True)
            self._logged_in = True
            return
        self._password_login()

    def _password_login(self) -> None:
        """用户名/邮箱 + 密码（+ TOTP 两步验证）登录；成功后把 Cookie 回写到账号，之后直接用 Cookie。"""
        if not self._has_password_login():
            raise CredentialMissing("没有可用的 Cookie，也没填用户名和密码，无法登录。请到「设置 → 账号 → 编辑」填写。")
        try:
            self._loop.run(self._login_flow(), timeout=120)
        except XClientError:
            raise
        except Exception as e:
            raise self._wrap_login_error(e)
        self._logged_in = True
        cookies = self._client.get_cookies() or {}
        if cookies.get("auth_token") and cookies.get("ct0"):
            self.creds["auth_token"] = cookies["auth_token"]
            self.creds["ct0"] = cookies["ct0"]
            if self._on_cookies:
                try:
                    self._on_cookies({"auth_token": cookies["auth_token"], "ct0": cookies["ct0"]})
                except Exception:
                    log.exception("回写 Cookie 失败")

    async def _login_flow(self) -> None:
        """自研登录流程（不用 twikit.Client.login）：按 X 每一步返回的 subtask 应答。

        twikit 自带的 login 在遇到「邮箱验证码」「选择两步验证方式」等步骤时会调用 input() 等待键盘输入，
        在后台服务里直接卡死或抛 EOFError——这就是密码登录"失效"的原因。这里把每一步都显式处理：
        能自动过的（密码、TOTP、备用标识、账号查重）自动过；过不了的（邮箱验证码、人机验证、被拒）
        抛出说明清楚的中文错误。"""
        from twikit.utils import Flow, find_dict
        try:
            from twikit.ui_metrics import solve_ui_metrics
        except Exception:  # pragma: no cover - 依赖缺失时退化为空应答
            solve_ui_metrics = None
        import pyotp

        c = self._client
        username = (self.creds.get("username") or "").strip().lstrip("@")
        email = (self.creds.get("email") or "").strip()
        password = self.creds["password"]
        totp_secret = normalize_totp_secret(self.creds.get("totp_secret"))

        def _hint(resp) -> str:
            try:
                return str(find_dict(resp, "secondary_text", find_one=True)[0]["text"])
            except Exception:
                return ""

        c.http.cookies.clear()
        guest_token = await c._get_guest_token()
        flow = Flow(c, guest_token)
        await flow.execute_task(params={"flow_name": "login"}, data=_LOGIN_FLOW_INIT)
        await flow.sso_init("apple")
        ui_metrics = ""
        if solve_ui_metrics is not None:
            try:
                ui_metrics = solve_ui_metrics(await c._ui_metrics())
            except Exception:
                ui_metrics = ""
        await flow.execute_task({"subtask_id": "LoginJsInstrumentationSubtask",
                                 "js_instrumentation": {"response": ui_metrics, "link": "next_link"}})
        await flow.execute_task({"subtask_id": "LoginEnterUserIdentifierSSO",
                                 "settings_list": {"setting_responses": [
                                     {"key": "user_identifier", "response_data": {"text_data": {"result": username}}}],
                                     "link": "next_link"}})

        alt_used = False
        for _ in range(12):
            task = flow.task_id
            if task is None or task in ("LoginSuccessSubtask", "LoginOpenHomeTimeline"):
                break
            if task == "LoginEnterAlternateIdentifierSubtask":
                if not email or alt_used:
                    raise AuthExpired("X 要求额外确认这个账号绑定的邮箱（或手机号）才肯继续登录。"
                                      "请到「设置 → 账号 → 编辑」把「邮箱」填上再试；如果填的还是不行，"
                                      "请先在浏览器登录一次该账号，然后改用 Cookie 方式。")
                alt_used = True
                await flow.execute_task({"subtask_id": task, "enter_text": {"text": email, "link": "next_link"}})
            elif task == "LoginEnterPassword":
                await flow.execute_task({"subtask_id": task, "enter_password": {"password": password, "link": "next_link"}})
            elif task == "LoginTwoFactorAuthChooseMethod":
                # 多种两步验证方式时，优先选「验证器 App」
                choices = find_dict(flow.response, "choices", find_one=True)
                choices = choices[0] if choices else []
                pick = None
                for ch in choices:
                    blob = (str(ch.get("id", "")) + " " + str(ch.get("text", ""))).lower()
                    if "totp" in blob or "authentic" in blob or "認証アプリ" in blob or "验证器" in blob:
                        pick = ch.get("id"); break
                if pick is None and choices:
                    pick = choices[0].get("id")
                await flow.execute_task({"subtask_id": task, "choice_selection": {
                    "link": "next_link", "selected_choices": [pick] if pick is not None else []}})
            elif task == "LoginTwoFactorAuthChallenge":
                if not totp_secret:
                    raise AuthExpired("这个账号开了两步验证（2FA），但账号里没填「TOTP 密钥」。"
                                      "请到「设置 → 账号 → 编辑」填上绑定验证器时 X 给的那串密钥（不是 6 位动态码）。")
                try:
                    code = pyotp.TOTP(totp_secret).now()
                except Exception as e:
                    raise AuthExpired(f"TOTP 密钥格式不对，算不出验证码（{e}）。请重新核对「TOTP 密钥」。")
                await flow.execute_task({"subtask_id": task, "enter_text": {"text": code, "link": "next_link"}})
            elif task == "AccountDuplicationCheck":
                await flow.execute_task({"subtask_id": task,
                                         "check_logged_in_account": {"link": "AccountDuplicationCheck_false"}})
            elif task == "LoginAcid":
                raise AuthExpired("X 这次要求输入发到邮箱的验证码（" + (_hint(flow.response) or "邮箱验证") + "）。"
                                  "本工具不支持自动收邮箱验证码。解决办法：先在浏览器里登录一次这个账号并完成验证"
                                  "（之后一段时间 X 通常不会再要），然后再点「测试连接」；或者直接改用 Cookie 方式。")
            elif task == "DenyLoginSubtask":
                raise AuthExpired("X 拒绝了本次登录：" + (_hint(flow.response) or "未说明原因") +
                                  "。常见原因：密码错误、登录环境可疑（新 IP / 代理）、账号被限制。"
                                  "建议先在浏览器登录一次，然后改用 Cookie 方式。")
            elif "Arkose" in task or "Captcha" in task:
                raise AuthExpired("X 要求人机验证（验证码拼图），程序无法自动通过。请换个网络/代理再试，"
                                  "或在浏览器登录后改用 Cookie 方式。")
            else:
                raise AuthExpired(f"登录流程遇到无法自动处理的步骤（{task}：{_hint(flow.response) or '无说明'}）。"
                                  "请在浏览器登录后改用 Cookie 方式。")

        cookies = c.get_cookies() or {}
        if not (cookies.get("auth_token") and cookies.get("ct0")):
            raise AuthExpired("登录流程走完了，但 X 没有下发登录 Cookie（auth_token/ct0），可能密码不对或账号被限制。"
                              "请在浏览器里确认能正常登录，然后重试或改用 Cookie 方式。")
        try:
            c._user_id = find_dict(flow.response, "id_str", find_one=True)[0]
        except Exception:
            pass

    def _wrap_login_error(self, e: Exception) -> XClientError:
        E = self._err
        msg = _short(str(e))
        low = msg.lower()
        if isinstance(e, E.TwitterException) or isinstance(e, E.BadRequest):
            if "password" in low or "密码" in low or "パスワード" in low:
                return AuthExpired(f"用户名或密码错误：{msg}")
            if "code" in low and ("incorrect" in low or "wrong" in low or "invalid" in low):
                return AuthExpired(f"两步验证码被拒绝：{msg}。请核对「TOTP 密钥」是否完整，以及电脑时间是否准确（TOTP 依赖系统时间）。")
            return AuthExpired(f"X 登录失败：{msg}。建议在浏览器登录一次后改用 Cookie 方式。")
        if isinstance(e, (asyncio.TimeoutError, TimeoutError, OSError)):
            return NetworkError(f"登录时网络超时/连接失败（{describe_proxy(self.proxy_used)}）：{msg}")
        return self._wrap(e, "账号密码登录")

    def _call(self, coro_factory, what: str):
        """coro_factory 是「返回协程的函数」，这样 Cookie 失效时可以重新登录后再调一次。"""
        self._ensure_login()
        try:
            return self._loop.run(coro_factory())
        except Exception as e:
            err = self._wrap(e, what)
            # Cookie 失效但有账号密码：自动重新登录一次再试
            if isinstance(err, AuthExpired) and self._has_password_login() and not self._relogin_tried:
                self._relogin_tried = True
                log.info("Cookie 失效，尝试用账号密码重新登录（%s）", what)
                self._logged_in = False
                self.creds["auth_token"] = ""
                self.creds["ct0"] = ""
                self._password_login()
                try:
                    return self._loop.run(coro_factory())
                except Exception as e2:
                    raise self._wrap(e2, what)
            raise err

    # ---- 异常映射 ----
    def _wrap(self, e: Exception, what: str) -> XClientError:
        E = self._err
        msg = _short(str(e))
        if isinstance(e, XClientError):
            return e
        if isinstance(e, E.TooManyRequests):
            return RateLimited(f"X 限流（{what}），稍后自动重试。{msg}", raw=e)
        if isinstance(e, (E.Unauthorized, E.AccountLocked, E.AccountSuspended)):
            return AuthExpired(f"Cookie 失效或账号被锁/封（{what}）：请在浏览器重新登录该账号并更新 auth_token/ct0。{msg}", raw=e)
        if isinstance(e, E.DuplicateTweet):
            return DuplicateContent(f"X 判定为重复内容，拒绝发送（{what}）。", raw=e)
        if isinstance(e, (E.Forbidden, E.CouldNotTweet)):
            return PermissionDenied(f"X 拒绝操作（{what}）：对方限制回复或账号被限写。{msg}", raw=e)
        if isinstance(e, (E.NotFound, E.UserNotFound, E.UserUnavailable, E.TweetNotAvailable)):
            return TargetNotFound(f"目标不存在或不可用（{what}）。{msg}", raw=e)
        if isinstance(e, E.InvalidMedia):
            return MediaError(f"媒体无效（{what}）。{msg}", raw=e)
        if isinstance(e, (E.ServerError, E.RequestTimeout)):
            return NetworkError(f"X 服务端/超时（{what}），稍后重试。{msg}", raw=e)
        if isinstance(e, E.BadRequest):
            return XClientError(f"请求被拒（{what}）：{msg}", raw=e)
        if isinstance(e, E.TwitterException):
            return XClientError(f"X 返回错误（{what}）：{msg}", raw=e)
        if isinstance(e, (asyncio.TimeoutError, TimeoutError, OSError)):
            return NetworkError(f"网络超时/连接失败（{what}）：{msg}", raw=e)
        if isinstance(e, (KeyError, IndexError, TypeError, AttributeError, ValueError)):
            # twikit 拿到非预期响应（多半是 Cookie 无效/过期，X 返回了登录页）
            return AuthExpired(f"X 返回了非预期内容（{what}），多半是 Cookie 无效或已过期：请在浏览器重新登录该账号，"
                               f"重新复制 auth_token 与 ct0。技术信息：{msg}", raw=e)
        return NetworkError(f"无法完成请求（{what}）。可能原因：① Cookie 无效或已过期（重新登录后复制 auth_token/ct0）；"
                            f"② 本机访问不了 x.com（需要在账号里填代理）。技术信息：{msg}", raw=e)

    # ---- 数据转换 ----
    @staticmethod
    def _to_tweet(tw) -> TweetData:
        created = getattr(tw, "created_at_datetime", None) or datetime.now(timezone.utc)
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        user = getattr(tw, "user", None)
        reply_to = getattr(tw, "in_reply_to", None)
        return TweetData(
            tweet_id=str(tw.id),
            author_id=str(getattr(user, "id", "") or ""),
            author_handle=str(getattr(user, "screen_name", "") or ""),
            text=getattr(tw, "full_text", None) or tw.text or "",
            lang=getattr(tw, "lang", None),
            created_at=created.astimezone(timezone.utc),
            is_retweet=getattr(tw, "retweeted_tweet", None) is not None,
            in_reply_to_tweet_id=str(reply_to) if reply_to else None,
        )

    @staticmethod
    def _since_filter(tweets: list[TweetData], since_id: str | None) -> FetchResult:
        if since_id:
            try:
                s = int(since_id)
                tweets = [t for t in tweets if int(t.tweet_id) > s]
            except ValueError:
                pass
        tweets.sort(key=lambda t: int(t.tweet_id))
        newest = tweets[-1].tweet_id if tweets else None
        return FetchResult(tweets=tweets, newest_id=newest, reads_consumed=len(tweets))

    # ---- 只读 ----
    def get_me(self) -> UserData:
        if self._me:
            return self._me
        u = self._call(lambda: self._client.user(), "获取本账号信息")
        self._me = UserData(user_id=str(u.id), handle=u.screen_name, display_name=u.name or "")
        return self._me

    def get_user_by_handle(self, handle: str) -> UserData:
        h = handle.lstrip("@").strip()
        u = self._call(lambda: self._client.get_user_by_screen_name(h), f"查询用户 @{h}")
        if u is None:
            raise TargetNotFound(f"找不到用户 @{h}")
        return UserData(user_id=str(u.id), handle=u.screen_name, display_name=u.name or "")

    def tweet_exists(self, tweet_id: str) -> bool | None:
        try:
            tw = self._call(lambda: self._client.get_tweet_by_id(tweet_id), "核实推文")
        except TargetNotFound:
            return False
        except XClientError:
            return None
        return tw is not None

    def get_user_tweets(self, user_id: str, since_id: str | None = None,
                        max_results: int = 5, include_replies: bool = False) -> FetchResult:
        kind = "Replies" if include_replies else "Tweets"
        res = self._call(lambda: self._client.get_user_tweets(user_id, kind, count=max(5, min(40, max_results))),
                         "拉取推主时间线")
        tweets = [self._to_tweet(t) for t in (res or [])]
        tweets = [t for t in tweets if not t.is_retweet]
        return self._since_filter(tweets, since_id)

    def search_recent(self, query: str, since_id: str | None = None,
                      start_time: datetime | None = None, max_results: int = 15) -> FetchResult:
        res = self._call(lambda: self._client.search_tweet(query, "Latest", count=max(10, min(50, max_results))),
                         "搜索推文")
        tweets = [self._to_tweet(t) for t in (res or [])]
        if start_time:
            tweets = [t for t in tweets if t.created_at >= start_time]
        return self._since_filter(tweets, since_id)

    # ---- 写 ----
    def post(self, text: str, media_ids: list[str] | None = None) -> PostResult:
        tw = self._call(lambda: self._client.create_tweet(text=text, media_ids=media_ids or None), "发推")
        return PostResult(tweet_id=str(tw.id))

    def reply(self, text: str, in_reply_to_tweet_id: str,
              media_ids: list[str] | None = None) -> PostResult:
        tw = self._call(lambda: self._client.create_tweet(text=text, media_ids=media_ids or None,
                                                  reply_to=in_reply_to_tweet_id), "回复推文")
        return PostResult(tweet_id=str(tw.id))

    def upload_media(self, file_path: str, media_type: str, alt_text: str | None = None) -> str:
        try:
            return str(self._call(lambda: self._client.upload_media(file_path, wait_for_completion=True), "上传媒体"))
        except XClientError as e:
            raise MediaError(f"媒体上传失败：{e}", raw=e)


def _short(s: str, n: int = 160) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[:n] + "…"
