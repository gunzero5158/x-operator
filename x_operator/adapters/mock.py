"""MockXClient：无需任何凭据、不碰真实 X 的假适配器（MVP 默认）。

设计目标：让整条流水线（监控/搜索抓取→打分→匹配→审核→发送）在浏览器里立即可测。
- 抓取类方法返回一批贴近真实获客场景的日语/中英样本推文（部分命中筛选条件、部分噪声）。
- 发送类方法只生成一个假 tweet_id 并返回，不产生任何外部副作用。

每次抓取从样本池轮转取几条、赋予全新递增 id：每按一次「运行」都能看到新结果，
同时 since_id 游标行为与真实适配器一致。
"""
from __future__ import annotations

import hashlib
import threading
import time
from datetime import datetime, timedelta, timezone

from .base import FetchResult, PostResult, TweetData, UserData, XClient

# 一批固定样本：混入正例（正在为 AI API 成本发愁）与噪声（新闻/教程/招聘/营销）。
# 每条给定相对「新鲜度」偏移（分钟），tweet_id 单调递增，模拟真实时间线。
_SAMPLE_TWEETS = [
    ("正例", "ja", "Stable DiffusionのAPI、個人開発だと従量課金がじわじわ効いてくる…もっと安く複数モデル試せる方法ないかな😭"),
    ("噪声", "ja", "【ニュース】OpenAIが新モデルを発表。APIの料金体系も改定されるとのこと。"),
    ("正例", "en", "honestly the LLM api bill this month hurt. anyone found a cheaper multi-model gateway that actually works?"),
    ("噪声", "ja", "【求人】AIエンジニア募集中！LLM API開発経験者優遇。リモート可。#エンジニア転職"),
    ("正例", "ja", "画像生成APIのコストが想像以上。個人だと月数万は厳しい。何かいい代替ないですか？"),
    ("噪声", "en", "Tutorial: how to call the OpenAI API in Python. Full guide with code examples on my blog."),
    ("正例", "ja", "LLMを複数使い分けたいけど、それぞれ契約するのが面倒でコストも読めない。まとめられたら楽なのに。"),
    ("噪声", "ja", "弊社の新AIサービスをリリースしました！ぜひお試しください🎉 #PR"),
    ("正例", "zh", "最近在做个小项目，几个大模型 API 费用加起来顶不住了，有没有按量计费还能统一管理的方案？"),
    ("噪声", "ja", "AIの倫理について考えるイベントを開催します。参加者募集中。"),
]


def _stable_id(seed: str) -> str:
    return str(int(hashlib.sha1(seed.encode()).hexdigest()[:12], 16))


_seq_lock = threading.Lock()
_seq = 0


def _next_id() -> str:
    """毫秒时间戳 + 递增序号：跨调用、跨实例都严格递增，配合 since_id 游标行为与真实 X 一致。"""
    global _seq
    with _seq_lock:
        _seq += 1
        return str(int(time.time() * 1000) * 1000 + (_seq % 1000))


class MockXClient(XClient):
    """每次抓取都「冒出」几条新样本推文（轮转取样），让用户每按一次「运行」都能看到新结果；
    发送类方法只返回假 id，不产生任何外部副作用。"""

    api_kind = "x_mock"

    def __init__(self, handle: str = "apimax_jp"):
        self._handle = handle
        self._rot = 0

    # --- 只读 ---
    def get_me(self) -> UserData:
        return UserData(user_id=_stable_id("me_" + self._handle), handle=self._handle, display_name="Mock 账号")

    def get_user_by_handle(self, handle: str) -> UserData:
        h = handle.lstrip("@")
        return UserData(user_id="mock_user_" + h, handle=h, display_name=h)

    def _fresh_batch(self, count: int, author_handle: str | None, author_id: str | None) -> list[TweetData]:
        """从样本池轮转取 count 条，时间戳落在最近几分钟内，id 严格递增（比任何旧游标都新）。"""
        now = datetime.now(timezone.utc)
        n = max(1, min(count, len(_SAMPLE_TWEETS)))
        tweets: list[TweetData] = []
        for i in range(n):
            _kind, lang, text = _SAMPLE_TWEETS[(self._rot + i) % len(_SAMPLE_TWEETS)]
            created = now - timedelta(seconds=(n - 1 - i) * 45)
            ah = author_handle or f"seeker_{(self._rot + i) % 97:02d}"
            aid = author_id or ("mock_user_" + ah)
            tweets.append(TweetData(
                tweet_id=_next_id(), author_id=aid, author_handle=ah, text=text, lang=lang,
                created_at=created, is_retweet=False, in_reply_to_tweet_id=None,
            ))
        self._rot = (self._rot + n) % len(_SAMPLE_TWEETS)
        return tweets

    @staticmethod
    def _result(tweets: list[TweetData], since_id: str | None) -> FetchResult:
        if since_id is not None:
            try:
                tweets = [t for t in tweets if int(t.tweet_id) > int(since_id)]
            except ValueError:
                pass
        newest = tweets[-1].tweet_id if tweets else None
        return FetchResult(tweets=tweets, newest_id=newest, reads_consumed=len(tweets))

    def get_user_tweets(self, user_id: str, since_id: str | None = None,
                        max_results: int = 5, include_replies: bool = False,
                        start_time: datetime | None = None) -> FetchResult:
        handle = user_id.replace("mock_user_", "") or "mock_user"
        return self._result(self._fresh_batch(3, handle, user_id), since_id)

    def search_recent(self, query: str, since_id: str | None = None,
                      start_time: datetime | None = None,
                      max_results: int = 15) -> FetchResult:
        return self._result(self._fresh_batch(min(max_results, 6), None, None), since_id)

    # --- 写 ---
    def post(self, text: str, media_ids: list[str] | None = None) -> PostResult:
        return PostResult(tweet_id="mock_post_" + _next_id())

    def reply(self, text: str, in_reply_to_tweet_id: str,
              media_ids: list[str] | None = None) -> PostResult:
        return PostResult(tweet_id="mock_reply_" + _next_id())

    def tweet_exists(self, tweet_id: str) -> bool | None:
        return True
