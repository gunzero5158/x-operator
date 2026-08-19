"""真实适配器占位（official=tweepy / unofficial=twifork）。

MVP 阶段这两个类只做「未实现」占位：接口签名与 design-v1.1 §3.3/§3.4 一致，
但方法体抛出中文的 CredentialMissing/未实现提示，指引用户接下来怎么接真实凭据。
测通 UI 交互后，把这里补成真正的 tweepy / twikit 调用即可，上层无需改动。
"""
from __future__ import annotations

from datetime import datetime

from .base import (CredentialMissing, FetchResult, PostResult, UserData,
                   XClient)

_OFFICIAL_HINT = ("官方适配器（tweepy）尚未在 MVP 中启用。接入方法：在设置页填入 X API 的 4 项凭据，"
                  "并把 adapters/real.py 的 OfficialXClient 补成真实 tweepy 调用（见 design-v1.1 §5.1）。")
_UNOFFICIAL_HINT = ("非官方适配器（twifork）尚未在 MVP 中启用。接入方法：命令行运行 scripts/login_helper.py "
                    "生成 cookies，再把 UnofficialXClient 补成真实 twikit 调用（见 design-v1.1 §5.2）。")


class _NotWired(XClient):
    hint = "未实现"
    api_kind = "x_official"

    def __init__(self, *args, **kwargs):
        pass

    def get_me(self) -> UserData:
        raise CredentialMissing(self.hint)

    def get_user_by_handle(self, handle: str) -> UserData:
        raise CredentialMissing(self.hint)

    def post(self, text, media_ids=None) -> PostResult:
        raise CredentialMissing(self.hint)

    def reply(self, text, in_reply_to_tweet_id, media_ids=None) -> PostResult:
        raise CredentialMissing(self.hint)

    def get_user_tweets(self, user_id, since_id=None, max_results=5, include_replies=False) -> FetchResult:
        raise CredentialMissing(self.hint)

    def search_recent(self, query, since_id=None, start_time: datetime | None = None, max_results=15) -> FetchResult:
        raise CredentialMissing(self.hint)


class OfficialXClient(_NotWired):
    hint = _OFFICIAL_HINT
    api_kind = "x_official"


class UnofficialXClient(_NotWired):
    hint = _UNOFFICIAL_HINT
    api_kind = "x_unofficial"
