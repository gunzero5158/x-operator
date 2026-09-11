"""适配器统一异常、数据类与 XClient 抽象基类（design-v1.1 §3.1/§3.2）。

异常语义 = 重试策略唯一依据：仅 NetworkError 与 RateLimited 可重试，其余终态。
所有 message 必须是中文人话（NFR-6）。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

from ..core.langdetect import refine_tweet_lang


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
class MediaData:
    """推文附件的元信息（不下载文件本身）。kind：photo / video / gif；
    preview_url：图片直链，视频 / GIF 则是 X 生成的预览图（封面）；duration_ms 仅视频有。"""
    kind: str
    preview_url: str
    duration_ms: int | None = None
    alt_text: str | None = None

    def as_dict(self) -> dict:
        d = {"kind": self.kind, "preview_url": self.preview_url}
        if self.duration_ms is not None:
            d["duration_ms"] = self.duration_ms
        if self.alt_text:
            d["alt_text"] = self.alt_text
        return d


MEDIA_KIND_LABEL = {"photo": "图片", "video": "视频", "gif": "GIF"}


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
    media: tuple[MediaData, ...] = ()   # 附件元信息（图片直链 / 视频预览图）；两条通道都不额外计费

    def __post_init__(self):
        # X 给中文推文基本只标 zh：按正文字形细分成简体 zh-Hans / 繁体 zh-Hant（看不出来保留 zh），各通道统一在这里做
        object.__setattr__(self, "lang", refine_tweet_lang(self.lang, self.text))


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
    dropped_low_views: int = 0     # 其中因观看量低于下限被丢掉的条数
    max_views_seen: int | None = None   # 扫描到的最高观看量（0 条达标时给用户调下限用）
    dropped_high_views: int = 0    # 其中因观看量高于上限被丢掉的条数
    min_views_seen: int | None = None   # 扫描到的最低观看量（0 条达标时给用户调上限用）


@dataclass
class ViewFilter:
    """观看量区间过滤 + 统计，各通道共用。min_views / max_views：0 = 不限。拿不到观看量的按 0 算
    （有下限时会被丢，只有上限时保留）。"""
    min_views: int = 0
    max_views: int = 0
    dropped_low: int = 0
    dropped_high: int = 0
    top_seen: int | None = None
    low_seen: int | None = None

    @property
    def active(self) -> bool:
        return bool(self.min_views or self.max_views)

    def keep(self, t: "TweetData") -> bool:
        v = t.view_count
        if v is not None:
            self.top_seen = v if self.top_seen is None else max(self.top_seen, v)
            self.low_seen = v if self.low_seen is None else min(self.low_seen, v)
        n = v or 0
        if self.min_views and n < self.min_views:
            self.dropped_low += 1
            return False
        if self.max_views and n > self.max_views:
            self.dropped_high += 1
            return False
        return True

    def result(self, kept: list["TweetData"], newest: str | None, scanned: int) -> "FetchResult":
        kept.sort(key=lambda t: int(t.tweet_id))
        return FetchResult(tweets=kept, newest_id=newest, reads_consumed=scanned, scanned=scanned,
                           dropped_low_views=self.dropped_low, max_views_seen=self.top_seen,
                           dropped_high_views=self.dropped_high, min_views_seen=self.low_seen)


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
                      scan_limit: int = 0, max_views: int = 0) -> FetchResult:
        """在抓取端做观看量区间过滤（min_views / max_views，0 = 不限）：
        有下限时改用「热门/相关度」排序（新发的推文观看量都低，按时间倒序凑不到）；只有上限时仍按时间排
        （热门排序捞出来的恰好是要丢的高观看量推文）。有任一门槛就一页不够继续翻页，直到凑够 max_results 条达标的，
        或累计扫描到 scan_limit 条（0 = 只扫一页）。区间外或不在 start_time 窗口内的不返回，但计入 scanned 和统计。"""

    def get_home_timeline(self, kind: str = "for_you", max_results: int = 50, min_views: int = 0,
                          scan_limit: int = 0, max_age_h: int | None = None, max_views: int = 0) -> FetchResult:
        """读本账号的首页时间线：kind='for_you' 推荐流 / 'following' 关注流。不含转推和回复；
        max_age_h 只要这么多小时内的；min_views/max_views/scan_limit 语义同 search_recent。官方 API 没有推荐流接口。"""
        raise XClientError("该通道不支持读取首页时间线")

    def upload_media(self, file_path: str, media_type: str,
                     alt_text: str | None = None) -> str:
        """上传附件，返回本账号当次有效的 media_id。media_type：image / gif / video。"""
        raise MediaError("该通道不支持上传附件")

    def tweet_exists(self, tweet_id: str) -> bool | None:
        """发送后核实：True=在 X 上能查到；False=查不到（被删/被限制）；None=无法判断。"""
        return None
