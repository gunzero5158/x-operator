"""MockXClient：无需任何凭据、不碰真实 X 的假适配器（MVP 默认）。

设计目标：让整条流水线（监控/搜索抓取→打分→匹配→审核→发送）在浏览器里立即可测。
- 抓取类方法返回一批贴近真实获客场景的日语/中英样本推文（部分命中筛选条件、部分噪声）。
- 发送类方法只生成一个假 tweet_id 并返回，不产生任何外部副作用。

样本推文按 since_id 做客户端过滤，配合游标推进，行为与真实适配器一致，便于把
MonitorJob/SearchJob 的游标逻辑测透。
"""
from __future__ import annotations

import hashlib
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


class MockXClient(XClient):
    api_kind = "x_mock"

    def __init__(self, handle: str = "apimax_jp"):
        self._handle = handle
        # 基准时间锚点：以当前时刻为最新推文，向前每条递减 7 分钟
        self._base = datetime.now(timezone.utc)

    # --- 只读 ---
    def get_me(self) -> UserData:
        return UserData(user_id=_stable_id("me_" + self._handle), handle=self._handle, display_name="Mock 账号")

    def get_user_by_handle(self, handle: str) -> UserData:
        h = handle.lstrip("@")
        return UserData(user_id="mock_user_" + h, handle=h, display_name=h)

    def _build_tweets(self, author_handle: str, author_id: str, count: int) -> list[TweetData]:
        tweets: list[TweetData] = []
        samples = _SAMPLE_TWEETS[:count]
        n = len(samples)
        for i, (_kind, lang, text) in enumerate(samples):
            # 越靠后越新：i=n-1 为最新
            age_min = (n - 1 - i) * 7
            created = self._base - timedelta(minutes=age_min)
            # tweet_id 随时间单调递增（用时间戳秒数保证可比较）
            tid = str(int(created.timestamp()) * 1000 + i)
            tweets.append(TweetData(
                tweet_id=tid,
                author_id=author_id,
                author_handle=author_handle,
                text=text,
                lang=lang,
                created_at=created,
                is_retweet=False,
                in_reply_to_tweet_id=None,
            ))
        # 按 created_at 升序（旧→新）
        tweets.sort(key=lambda t: t.created_at)
        return tweets

    def _filter_since(self, tweets: list[TweetData], since_id: str | None) -> FetchResult:
        if since_id is not None:
            tweets = [t for t in tweets if int(t.tweet_id) > int(since_id)]
        newest = tweets[-1].tweet_id if tweets else None
        return FetchResult(tweets=tweets, newest_id=newest, reads_consumed=len(tweets))

    def get_user_tweets(self, user_id: str, since_id: str | None = None,
                        max_results: int = 5, include_replies: bool = False) -> FetchResult:
        handle = user_id.replace("mock_user_", "") or "mock_user"
        tweets = self._build_tweets(handle, user_id, max(max_results, 5))
        if since_id is None:
            # 首次设游标：只取最新 1 条
            latest = tweets[-1:]
            newest = latest[-1].tweet_id if latest else None
            return FetchResult(tweets=latest, newest_id=newest, reads_consumed=len(latest))
        return self._filter_since(tweets, since_id)

    def search_recent(self, query: str, since_id: str | None = None,
                      start_time: datetime | None = None,
                      max_results: int = 15) -> FetchResult:
        # 搜索场景返回多作者混合样本
        tweets = self._build_tweets("various_user", "mock_user_various", min(max(max_results, 10), len(_SAMPLE_TWEETS)))
        # 给每条一个不同作者，模拟广域搜索
        varied: list[TweetData] = []
        for i, t in enumerate(tweets):
            ah = f"seeker_{i:02d}"
            varied.append(TweetData(
                tweet_id=t.tweet_id, author_id="mock_user_" + ah, author_handle=ah,
                text=t.text, lang=t.lang, created_at=t.created_at,
                is_retweet=t.is_retweet, in_reply_to_tweet_id=t.in_reply_to_tweet_id,
            ))
        return self._filter_since(varied, since_id)

    # --- 写 ---
    def post(self, text: str, media_ids: list[str] | None = None) -> PostResult:
        return PostResult(tweet_id="mock_post_" + _stable_id(text + self._base.isoformat()))

    def reply(self, text: str, in_reply_to_tweet_id: str,
              media_ids: list[str] | None = None) -> PostResult:
        return PostResult(tweet_id="mock_reply_" + _stable_id(text + in_reply_to_tweet_id))
