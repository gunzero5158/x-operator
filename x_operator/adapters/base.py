"""适配器统一异常、数据类与 XClient 抽象基类（design-v1.1 §3.1/§3.2）。

异常语义 = 重试策略唯一依据：仅 NetworkError 与 RateLimited 可重试，其余终态。
所有 message 必须是中文人话（NFR-6）。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime


class XClientError(Exception):
    """所有适配器异常基类。"""

    def __init__(self, message: str, *, raw: Exception | None = None):
        super().__init__(message)
        self.raw = raw


class RateLimited(XClientError):
    def __init__(self, message: str, *, reset_at: datetime | None = None, raw: Exception | None = None):
        super().__init__(message, raw=raw)
        self.reset_at = reset_at


class AuthExpired(XClientError):
    """401/凭据失效/cookies 过期。非官方通道填了账号密码时适配器内部会重新登录一次；
    仍失败才抛出，捕获方将账号置 auth_error，不再重试。"""


class DuplicateContent(XClientError):
    """X 判定重复内容。条目置 failed，不重试。"""


class PermissionDenied(XClientError):
    """403 非鉴权类：对方锁推/禁止回复/被限写。不重试。"""


class TargetNotFound(XClientError):
    """目标推文/用户不存在。不重试。"""


class MediaError(XClientError):
    """媒体上传失败。不重试。"""


class NetworkError(XClientError):
    """网络/超时/5xx。可重试（指数退避 ≤2 次）。"""


class CredentialMissing(XClientError):
    """凭据未配置。"""


RETRYABLE = (RateLimited, NetworkError)


@dataclass(frozen=True)
class TweetData:
    tweet_id: str
    author_id: str
    author_handle: str
    text: str
    lang: str | None
    created_at: datetime
    is_retweet: bool
    in_reply_to_tweet_id: str | None
    view_count: int | None = None   # 观看量（官方 impression_count / 非官方 view_count）；拿不到为 None


@dataclass(frozen=True)
class UserData:
    user_id: str
    handle: str
    display_name: str


@dataclass(frozen=True)
class FetchResult:
    tweets: list[TweetData]
    newest_id: str | None          # 本次「扫描到」的最新 id（含被观看量门槛丢掉的），游标推进用
    reads_consumed: int
    scanned: int = 0               # 搜索时实际扫描的条数（翻页累计）
    dropped_low_views: int = 0     # 其中因观看量低于门槛被丢掉的条数


@dataclass(frozen=True)
class PostResult:
    tweet_id: str


class XClient(ABC):
    """所有方法均为同步阻塞；单实例串行调用（分发器按账号串行）。"""

    api_kind: str = "x_mock"

    @abstractmethod
    def get_me(self) -> UserData: ...

    @abstractmethod
    def get_user_by_handle(self, handle: str) -> UserData: ...

    @abstractmethod
    def post(self, text: str, media_ids: list[str] | None = None) -> PostResult: ...

    @abstractmethod
    def reply(self, text: str, in_reply_to_tweet_id: str,
              media_ids: list[str] | None = None) -> PostResult: ...

    @abstractmethod
    def get_user_tweets(self, user_id: str, since_id: str | None = None,
                        max_results: int = 5, include_replies: bool = False,
                        start_time: datetime | None = None) -> FetchResult:
        """since_id 优先；没有游标时用 start_time 限定「首次回溯」窗口（官方 API 按返回条数计费，
        所以要把窗口交给服务端，而不是拉一堆再本地丢）。"""

    @abstractmethod
    def search_recent(self, query: str, since_id: str | None = None,
                      start_time: datetime | None = None,
                      max_results: int = 15, min_views: int = 0,
                      scan_limit: int = 0) -> FetchResult:
        """min_views>0 时在抓取端做观看量门槛：一页不够就继续翻页，直到凑够 max_results 条达标的，
        或累计扫描到 scan_limit 条（0 = 只扫一页）。低于门槛的不返回，但计入 newest_id 和 scanned。"""

    def upload_media(self, file_path: str, media_type: str,
                     alt_text: str | None = None) -> str:
        raise MediaError("MVP 暂不支持媒体上传")

    def tweet_exists(self, tweet_id: str) -> bool | None:
        """发送后核实：True=在 X 上能查到；False=查不到（被删/被限制）；None=无法判断。"""
        return None
