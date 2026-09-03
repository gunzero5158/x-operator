"""MonitorJob（design-v1.1 §7.4 / v1.0 §8.1）：轮询监控推主的新推文。

流程：对每个 enabled 推主，用官方账号 get_user_tweets(since_id=游标) 拉新推 →
预检过滤（转推/黑名单/已回复/冷却/太旧/自有账号）→ 存 target_tweets →
通过预检的交给 MatchEngine → 推进游标。单推主异常不影响其余。

时间窗：没有游标（首次/重置后）时把「首次回溯」交给适配器的 start_time（官方 API 按返回条数计费，
窗口交给服务端才不会白花钱）；有游标后只拉游标之后的。每次最多拉 MAX_FETCH 条。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .. import config
from ..adapters import factory
from ..adapters.base import TweetData, XClientError
from ..db.database import get_conn, to_iso, utcnow_iso
from . import budget
from .compliance import is_blacklisted
from .matcher import MatchEngine

# 单个推主一次最多拉多少条（官方 API 单页上限 100，非官方 40）。高产推主一天几十条也够；
# 更早的会在下一轮凭游标继续，不会丢
MAX_FETCH = 100


@dataclass
class MonitorStats:
    users_polled: int = 0
    tweets_fetched: int = 0
    queued: int = 0
    no_match: int = 0
    filtered: int = 0
    errors: int = 0
    notes: list[str] = field(default_factory=list)   # 中文说明（为什么没结果 / 哪个推主出错）

    @property
    def ok(self) -> bool:
        return self.errors == 0 and not (self.users_polled == 0 and self.notes)

    def as_msg(self) -> str:
        head = (f"监控完成：轮询 {self.users_polled} 位推主，拉取 {self.tweets_fetched} 条，"
                f"入队 {self.queued}，未匹配 {self.no_match}，过滤 {self.filtered}，错误 {self.errors}")
        if self.notes:
            head += "。" + "；".join(self.notes[:3])
        return head


READ_CHANNEL_LABEL = {"unofficial": "小号 Cookie 通道（免费，读额度不生效）", "official": "官方 API（按条计费，读额度生效）"}


def read_channel() -> str:
    v = (config.get("read_channel") or "unofficial").strip()
    return v if v in READ_CHANNEL_LABEL else "unofficial"


def get_read_account() -> sqlite3.Row | None:
    """抓取（监控/搜索的读取）用的账号，按设置里的「抓取通道」选：
    - 小号通道（默认）：在启用中的非官方账号里挑今天读得最少的（多个小号分摊风控）；一个都没有就退回官方号。
    - 官方 API：优先主号；没有官方号就退回小号。
    调用方用 account['access_type'] 判断这次读取是否计费/是否受读额度限制。"""
    with get_conn() as conn:
        official = conn.execute(
            "SELECT * FROM accounts WHERE status='active' AND access_type='official' "
            "ORDER BY is_primary DESC, id ASC LIMIT 1").fetchone()
        unofficial = conn.execute(
            "SELECT a.* FROM accounts a LEFT JOIN ("
            "  SELECT account_id, SUM(reads_consumed) AS r FROM action_log "
            "  WHERE created_at>=strftime('%Y-%m-%dT00:00:00Z','now') GROUP BY account_id) l ON l.account_id=a.id "
            "WHERE a.status='active' AND a.access_type='unofficial' "
            "ORDER BY COALESCE(l.r,0) ASC, a.id ASC LIMIT 1").fetchone()
    if read_channel() == "official":
        return official or unofficial
    return unofficial or official


# 旧名字，其他模块还在用
get_primary_account = get_read_account


def read_is_billed(account: sqlite3.Row) -> bool:
    return account["access_type"] == "official"


def _row_int(row: sqlite3.Row, key: str, default: int) -> int:
    try:
        v = row[key]
    except (IndexError, KeyError):
        return default
    try:
        return int(v) if v is not None else default
    except (TypeError, ValueError):
        return default


FILTER_REASONS = {
    "retweet": "转推，跳过",
    "own_account": "自己账号的推文，跳过",
    "too_old": "推文早于「首次回溯」时间窗",
    "blacklisted": "作者在黑名单",
    "already_replied": "该推文已回复过（去重账本）",
    "author_cooldown": "作者处于冷却期（近期已互动过）",
}


def precheck(t: TweetData, account_handle: str, max_age_h: int | None = None) -> str | None:
    """返回过滤原因码或 None。max_age_h = 该规则/推主自己的时间窗（小时）；None = 不按年龄卡（有游标时）。"""
    if t.is_retweet:
        return "retweet"
    if t.author_handle and t.author_handle.lower() == (account_handle or "").lower():
        return "own_account"
    if max_age_h and t.created_at < datetime.now(timezone.utc) - timedelta(hours=max_age_h):
        return "too_old"
    with get_conn() as conn:
        if is_blacklisted(conn, t.author_id, t.author_handle):
            return "blacklisted"
        if conn.execute("SELECT 1 FROM interactions WHERE action='reply' AND tweet_id=?", (t.tweet_id,)).fetchone():
            return "already_replied"
        cooldown_days = config.get_int("cooldown_days", 7)
        cutoff = to_iso(datetime.now(timezone.utc) - timedelta(days=cooldown_days))
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
                "INSERT INTO target_tweets(tweet_id, author_id, author_handle, text, lang, view_count, "
                "tweet_created_at, source, source_rule_id, llm_relevance_score, llm_relevance_reason, "
                "process_status, fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (t.tweet_id, t.author_id, t.author_handle, t.text, t.lang, t.view_count,
                 to_iso(t.created_at), source, source_rule_id,
                 score, reason, process_status, utcnow_iso()),
            )
            conn.commit()
            return cur.lastrowid
        except sqlite3.IntegrityError:
            return None


class MonitorJob:
    def __init__(self, match_engine: MatchEngine):
        self.match = match_engine

    def run_once(self, auto: bool = False, progress=None) -> MonitorStats:
        """auto=True 表示后台自动轮询（读额度熔断更保守）；手动按钮触发传 False。
        progress(0~1, 文字)：可选进度回调，UI 进度框用。"""
        stats = MonitorStats()
        account = get_read_account()
        if account is None:
            stats.notes.append("没有状态为「启用」的账号，无法抓取。请到「设置 → 账号」添加并启用一个账号")
            return stats
        if read_is_billed(account):
            denied = budget.current().allow(auto)
            if denied:
                stats.notes.append(denied)
                return stats
        try:
            client = factory.get_client(account)
        except XClientError as e:
            stats.errors += 1
            stats.notes.append(f"账号 @{account['handle']} 无法连接：{e}")
            _log_read(account["id"], "x_official" if read_is_billed(account) else "x_unofficial",
                      "get_user_tweets", 0, success=False, error=str(e))
            return stats
        except ValueError as e:  # 主号误配非官方通道
            stats.errors += 1
            stats.notes.append(f"账号 @{account['handle']}：{e}")
            return stats

        with get_conn() as conn:
            users = conn.execute("SELECT * FROM watched_users WHERE enabled=1").fetchall()
        if not users:
            stats.notes.append("没有启用的监控推主。请到「监控推主」页添加")
            return stats

        total = len(users)

        def _p(i: int, sub: float, text: str) -> None:
            if progress:
                progress((i + sub) / total, f"（{i + 1}/{total}）" + text)

        for i, user in enumerate(users):
            stats.users_polled += 1
            lookback_h = _row_int(user, "lookback_hours", 24)
            cursor = user["last_seen_tweet_id"]
            start_time = None if (cursor or not lookback_h) else datetime.now(timezone.utc) - timedelta(hours=lookback_h)
            try:
                _p(i, 0.05, f"@{user['handle']}：正在从 X 拉取（{'游标之后的新推文' if cursor else f'最近 {lookback_h} 小时'}）…")
                result = client.get_user_tweets(user["x_user_id"], since_id=cursor, max_results=MAX_FETCH,
                                                include_replies=bool(user["include_replies"]), start_time=start_time)
                _log_read(account["id"], client.api_kind, "get_user_tweets", result.reads_consumed)
                tweets = result.tweets
                _p(i, 0.4, f"@{user['handle']}：拉到 {len(tweets)} 条，正在预检和生成回复…")
                # 首次抓取（没有游标）只看时间窗内的（适配器已尽量在服务端限定，这里兜底再筛一遍）
                if start_time:
                    dropped = [t for t in tweets if t.created_at < start_time]
                    tweets = [t for t in tweets if t.created_at >= start_time]
                    if dropped and not tweets:
                        stats.notes.append(f"@{user['handle']} 最近 {lookback_h} 小时内没有新推文（更早的 {len(dropped)} 条按时间窗跳过，可在推主设置里调大「首次回溯」）")
                stats.tweets_fetched += len(tweets)
                hit = 0
                for k, t in enumerate(tweets):
                    _p(i, 0.4 + 0.6 * k / max(1, len(tweets)), f"@{user['handle']}：处理第 {k + 1}/{len(tweets)} 条…")
                    reason = precheck(t, account["handle"], max_age_h=None if cursor else lookback_h)
                    if reason:
                        store_target(t, "monitor", user["id"], process_status="filtered",
                                     reason="预检拦下：" + FILTER_REASONS.get(reason, reason))
                        stats.filtered += 1
                        continue
                    tid = store_target(t, "monitor", user["id"], process_status="new")
                    if tid is None:
                        continue
                    with get_conn() as conn:
                        target = conn.execute("SELECT * FROM target_tweets WHERE id=?", (tid,)).fetchone()
                    outcome = self.match.run(target, account, cfg=user)
                    if outcome.status == "queued":
                        stats.queued += 1
                        hit += 1
                    else:
                        stats.no_match += 1
                # 推进游标 + 命中计数
                if result.newest_id:
                    with get_conn() as conn:
                        conn.execute(
                            "UPDATE watched_users SET last_seen_tweet_id=?, hit_count=hit_count+? WHERE id=?",
                            (result.newest_id, hit, user["id"]))
                        conn.commit()
            except XClientError as e:
                stats.errors += 1
                stats.notes.append(f"@{user['handle']}：{e}")
                _log_read(account["id"], client.api_kind, "get_user_tweets", 0, success=False, error=str(e))
            except Exception as e:  # 单推主隔离
                stats.errors += 1
                stats.notes.append(f"@{user['handle']}：{e}")
                _log_read(account["id"], client.api_kind, "get_user_tweets", 0, success=False, error=str(e))
        if stats.tweets_fetched == 0 and stats.errors == 0 and stats.users_polled:
            stats.notes.append("推主自上次游标之后没有新推文（可在推主卡片上「重置游标」重新抓最近几条）")
        stats.notes.append(f"本次用 @{account['handle']} 抓取（{'官方 API，计费' if read_is_billed(account) else '小号通道，不计费'}）")
        if progress:
            progress(1.0, "完成")
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
