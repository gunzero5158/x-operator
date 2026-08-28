"""真实适配器：official=tweepy（X API v2）/ unofficial=twifork（twikit 分支，Cookie 登录）。

凭据来自 accounts.credentials（JSON，设置页「账号」里填）：
- official  : consumer_key / consumer_secret / access_token / access_token_secret（必填 4 项）
              bearer_token（选填，只读接口优先用它省用户额度）
- unofficial: auth_token + ct0（浏览器 Cookie，推荐）；或 username/email/password(+totp_secret)
              走账号密码登录，成功后自动把 Cookie 写回账号，下次免登录。proxy 选填。

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
            api = t.API(auth)
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
        proxy = (self.creds.get("proxy") or "").strip() or None
        self._client = twikit.Client(language="ja", proxy=proxy)
        self._logged_in = False
        self._me: UserData | None = None

    # ---- 登录 ----
    def _ensure_login(self) -> None:
        if self._logged_in:
            return
        auth_token = (self.creds.get("auth_token") or "").strip()
        ct0 = (self.creds.get("ct0") or "").strip()
        if auth_token and ct0:
            self._client.set_cookies({"auth_token": auth_token, "ct0": ct0}, clear_cookies=True)
            self._logged_in = True
            return
        # 账号密码登录（更容易触发风控；成功后回写 Cookie，之后不再走这里）
        try:
            self._loop.run(self._client.login(
                auth_info_1=self.creds["username"].strip(),
                auth_info_2=(self.creds.get("email") or "").strip() or None,
                password=self.creds["password"],
                totp_secret=(self.creds.get("totp_secret") or "").strip() or None,
            ))
        except Exception as e:
            raise self._wrap(e, "账号密码登录")
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

    def _call(self, coro, what: str):
        self._ensure_login()
        try:
            return self._loop.run(coro)
        except Exception as e:
            raise self._wrap(e, what)

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
        u = self._call(self._client.user(), "获取本账号信息")
        self._me = UserData(user_id=str(u.id), handle=u.screen_name, display_name=u.name or "")
        return self._me

    def get_user_by_handle(self, handle: str) -> UserData:
        h = handle.lstrip("@").strip()
        u = self._call(self._client.get_user_by_screen_name(h), f"查询用户 @{h}")
        if u is None:
            raise TargetNotFound(f"找不到用户 @{h}")
        return UserData(user_id=str(u.id), handle=u.screen_name, display_name=u.name or "")

    def get_user_tweets(self, user_id: str, since_id: str | None = None,
                        max_results: int = 5, include_replies: bool = False) -> FetchResult:
        kind = "Replies" if include_replies else "Tweets"
        res = self._call(self._client.get_user_tweets(user_id, kind, count=max(5, min(40, max_results))),
                         "拉取推主时间线")
        tweets = [self._to_tweet(t) for t in (res or [])]
        tweets = [t for t in tweets if not t.is_retweet]
        return self._since_filter(tweets, since_id)

    def search_recent(self, query: str, since_id: str | None = None,
                      start_time: datetime | None = None, max_results: int = 15) -> FetchResult:
        res = self._call(self._client.search_tweet(query, "Latest", count=max(10, min(50, max_results))),
                         "搜索推文")
        tweets = [self._to_tweet(t) for t in (res or [])]
        if start_time:
            tweets = [t for t in tweets if t.created_at >= start_time]
        return self._since_filter(tweets, since_id)

    # ---- 写 ----
    def post(self, text: str, media_ids: list[str] | None = None) -> PostResult:
        tw = self._call(self._client.create_tweet(text=text, media_ids=media_ids or None), "发推")
        return PostResult(tweet_id=str(tw.id))

    def reply(self, text: str, in_reply_to_tweet_id: str,
              media_ids: list[str] | None = None) -> PostResult:
        tw = self._call(self._client.create_tweet(text=text, media_ids=media_ids or None,
                                                  reply_to=in_reply_to_tweet_id), "回复推文")
        return PostResult(tweet_id=str(tw.id))

    def upload_media(self, file_path: str, media_type: str, alt_text: str | None = None) -> str:
        try:
            return str(self._call(self._client.upload_media(file_path, wait_for_completion=True), "上传媒体"))
        except XClientError as e:
            raise MediaError(f"媒体上传失败：{e}", raw=e)


def _short(s: str, n: int = 160) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[:n] + "…"
