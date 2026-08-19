"""MonitorJob（design-v1.1 §7.4 / v1.0 §8.1）：轮询监控推主的新推文。

流程：对每个 enabled 推主，用官方账号 get_user_tweets(since_id=游标) 拉新推 →
预检过滤（转推/黑名单/已回复/冷却/太旧/自有账号）→ 存 target_tweets →
通过预检的交给 MatchEngine → 推进游标。单推主异常不影响其余。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .. import config
from ..adapters import factory
from ..adapters.base import TweetData, XClientError
from ..db.database import get_conn, utcnow_iso
from .matcher import MatchEngine


@dataclass
class MonitorStats:
    users_polled: int = 0
    tweets_fetched: int = 0
    queued: int = 0
    filtered: int = 0
    errors: int = 0

    def as_msg(self) -> str:
        return (f"监控完成：轮询 {self.users_polled} 位推主，拉取 {self.tweets_fetched} 条，"
                f"入队 {self.queued}，过滤 {self.filtered}，错误 {self.errors}")


def get_primary_account() -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM accounts WHERE status='active' AND access_type='official' "
            "ORDER BY is_primary DESC, id ASC LIMIT 1"
        ).fetchone()


def precheck(t: TweetData, account_handle: str) -> str | None:
    """返回过滤原因码或 None。"""
    if t.is_retweet:
        return "retweet"
    if t.author_handle == account_handle:
        return "own_account"
    max_age_h = config.get_int("tweet_max_age_hours", 48)
    if t.created_at < datetime.now(timezone.utc) - timedelta(hours=max_age_h):
        return "too_old"
    with get_conn() as conn:
        if conn.execute("SELECT 1 FROM blacklist WHERE x_user_id=?", (t.author_id,)).fetchone():
            return "blacklisted"
        if conn.execute("SELECT 1 FROM interactions WHERE action='reply' AND tweet_id=?", (t.tweet_id,)).fetchone():
            return "already_replied"
        cooldown_days = config.get_int("cooldown_days", 7)
        cutoff = (datetime.now(timezone.utc) - timedelta(days=cooldown_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        if conn.execute("SELECT 1 FROM interactions WHERE author_id=? AND sent_at>=? LIMIT 1",
                        (t.author_id, cutoff)).fetchone():
            return "author_cooldown"
    return None


def store_target(t: TweetData, source: str, source_rule_id: int | None,
                 process_status: str = "new", score: int | None = None,
                 reason: str | None = None) -> int | None:
    """写入 target_tweets（tweet_id 唯一，冲突则跳过返回 None）。"""
    with get_conn() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO target_tweets(tweet_id, author_id, author_handle, text, lang, "
                "tweet_created_at, source, source_rule_id, llm_relevance_score, llm_relevance_reason, "
                "process_status, fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (t.tweet_id, t.author_id, t.author_handle, t.text, t.lang,
                 t.created_at.strftime("%Y-%m-%dT%H:%M:%SZ"), source, source_rule_id,
                 score, reason, process_status, utcnow_iso()),
            )
            conn.commit()
            return cur.lastrowid
        except sqlite3.IntegrityError:
            return None


class MonitorJob:
    def __init__(self, match_engine: MatchEngine):
        self.match = match_engine

    def run_once(self) -> MonitorStats:
        stats = MonitorStats()
        account = get_primary_account()
        if account is None:
            return stats
        client = factory.get_client(account)

        with get_conn() as conn:
            users = conn.execute("SELECT * FROM watched_users WHERE enabled=1").fetchall()

        for user in users:
            stats.users_polled += 1
            try:
                result = client.get_user_tweets(user["x_user_id"], since_id=user["last_seen_tweet_id"],
                                                max_results=5, include_replies=bool(user["include_replies"]))
                stats.tweets_fetched += len(result.tweets)
                _log_read(account["id"], client.api_kind, "get_user_tweets", result.reads_consumed)
                hit = 0
                for t in result.tweets:
                    reason = precheck(t, account["handle"])
                    if reason:
                        store_target(t, "monitor", user["id"], process_status="filtered", reason=reason)
                        stats.filtered += 1
                        continue
                    tid = store_target(t, "monitor", user["id"], process_status="new")
                    if tid is None:
                        continue
                    with get_conn() as conn:
                        target = conn.execute("SELECT * FROM target_tweets WHERE id=?", (tid,)).fetchone()
                    outcome = self.match.run(target, account)
                    if outcome.status == "queued":
                        stats.queued += 1
                        hit += 1
                # 推进游标 + 命中计数
                if result.newest_id:
                    with get_conn() as conn:
                        conn.execute(
                            "UPDATE watched_users SET last_seen_tweet_id=?, hit_count=hit_count+? WHERE id=?",
                            (result.newest_id, hit, user["id"]))
                        conn.commit()
            except XClientError as e:
                stats.errors += 1
                _log_read(account["id"], client.api_kind, "get_user_tweets", 0, success=False, error=str(e))
            except Exception as e:  # 单推主隔离
                stats.errors += 1
                _log_read(account["id"], client.api_kind, "get_user_tweets", 0, success=False, error=str(e))
        return stats


def _log_read(account_id: int, api_kind: str, endpoint: str, reads: int,
              success: bool = True, error: str | None = None) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO action_log(account_id, api_kind, endpoint, reads_consumed, success, error, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (account_id, api_kind, endpoint, reads, 1 if success else 0, error, utcnow_iso()),
        )
        conn.commit()
