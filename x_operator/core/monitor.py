"""MonitorJob（design-v1.1 §7.4 / v1.0 §8.1）：轮询监控推主的新推文。

流程：对每个 enabled 推主，用官方账号 get_user_tweets(since_id=游标) 拉新推 →
预检过滤（转推/黑名单/已回复/冷却/太旧/自有账号）→ 存 target_tweets →
通过预检的交给 MatchEngine → 推进游标。单推主异常不影响其余。

时间窗：没有游标（首次/重置后）时把「首次回溯」交给适配器的 start_time（官方 API 按返回条数计费，
窗口交给服务端才不会白花钱）；有游标后只拉游标之后的。每次最多拉 MAX_FETCH 条。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .. import config
from ..adapters import factory
from ..adapters.base import RateLimited, TweetData, XClientError
from ..db.database import get_conn, parse_iso, to_iso, utcnow_iso
from .compliance import is_blacklisted
from .matcher import MatchEngine
from .readpool import ReadPool, resume_delay

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
    paused: bool = False                             # 因 429 / 账号都到限额而中途停下（会自动续跑）
    notes: list[str] = field(default_factory=list)   # 中文说明（为什么没结果 / 哪个推主出错）

    @property
    def ok(self) -> bool:
        return self.errors == 0 and not self.paused and not (self.users_polled == 0 and self.notes)

    def as_msg(self) -> str:
        head = (f"{'监控暂停' if self.paused else '监控完成'}：轮询 {self.users_polled} 位推主，拉取 {self.tweets_fetched} 条，"
                f"入队 {self.queued}，未匹配 {self.no_match}，过滤 {self.filtered}，错误 {self.errors}")
        if self.notes:
            head += "。\n" + "\n".join(self.notes[:12])
        return head


def get_read_account() -> sqlite3.Row | None:
    """现在这一刻读取账号池会挑出的账号（仪表盘展示 / 单次读取用）。正式抓取循环里每次请求都重新挑，见 core/readpool.py。"""
    return ReadPool().pick()[0]


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
                "INSERT INTO target_tweets(tweet_id, author_id, author_handle, text, lang, view_count, media, "
                "tweet_created_at, source, source_rule_id, llm_relevance_score, llm_relevance_reason, "
                "process_status, fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (t.tweet_id, t.author_id, t.author_handle, t.text, t.lang, t.view_count,
                 json.dumps([m.as_dict() for m in t.media], ensure_ascii=False),
                 to_iso(t.created_at), source, source_rule_id,
                 score, reason, process_status, utcnow_iso()),
            )
            conn.commit()
            return cur.lastrowid
        except sqlite3.IntegrityError:
            return None


class MonitorJob:
    # 暂停 / 续跑的记录放在 app_settings 里（进程重启也不丢）
    RESUME_FROM_KEY = "monitor_resume_from_user_id"   # 下次从哪个推主继续（watched_users.id）
    RESUME_AT_KEY = "monitor_resume_at"               # 什么时候自动继续（UTC ISO）；空 = 没有待续跑

    def __init__(self, match_engine: MatchEngine):
        self.match = match_engine
        self._lock = threading.Lock()

    @staticmethod
    def pending_resume() -> tuple[int | None, datetime | None]:
        """(从哪个推主继续, 何时继续)；都为 None = 上次跑完了。"""
        raw_id = config.get(MonitorJob.RESUME_FROM_KEY) or ""
        raw_at = config.get(MonitorJob.RESUME_AT_KEY) or ""
        uid = int(raw_id) if raw_id.isdigit() else None
        return uid, (parse_iso(raw_at) if raw_at else None)

    @staticmethod
    def _clear_resume() -> None:
        config.set_value(MonitorJob.RESUME_FROM_KEY, "")
        config.set_value(MonitorJob.RESUME_AT_KEY, "")

    def resume_if_due(self) -> MonitorStats | None:
        """调度器每分钟问一次：上次因 429 / 限额停下的运行到点了就接着跑。没到点或没有待续跑返回 None。"""
        uid, at = self.pending_resume()
        if uid is None or at is None or at > datetime.now(timezone.utc):
            return None
        return self.run_once(auto=True)

    def run_once(self, auto: bool = False, progress=None) -> MonitorStats:
        """auto=True 表示后台自动轮询（官方号的读额度熔断更保守）；手动按钮触发传 False。
        progress(0~1, 文字)：可选进度回调，UI 进度框用。

        每个推主请求前从读取账号池挑号（core/readpool.py）；挑不到号或撞到 429 就停下本次运行，
        记住下次从哪个推主继续，rate_limit_pause_minutes 后由调度器自动续跑。"""
        stats = MonitorStats()
        if not self._lock.acquire(blocking=False):
            stats.notes.append("监控正在运行中，这次不重复启动")
            return stats
        try:
            return self._run(stats, auto, progress)
        finally:
            self._lock.release()

    def _run(self, stats: MonitorStats, auto: bool, progress) -> MonitorStats:
        pool = ReadPool(auto=auto)
        with get_conn() as conn:
            users = conn.execute("SELECT * FROM watched_users WHERE enabled=1 ORDER BY id").fetchall()
        if not users:
            acc, why = pool.pick()
            stats.notes.append(why if acc is None else "没有启用的监控推主。请到「监控推主」页添加")
            self._clear_resume()
            return stats
        # 上次没跑完：从记住的推主开始，转一圈
        resume_from, _at = self.pending_resume()
        if resume_from is not None:
            k = next((i for i, u in enumerate(users) if u["id"] >= resume_from), 0)
            if k:
                users = users[k:] + users[:k]
                stats.notes.append(f"接着上次停下的地方继续（从 @{users[0]['handle']} 开始）")
        total = len(users)
        clients: dict[int, object] = {}

        def _p(i: int, sub: float, text: str) -> None:
            if progress:
                progress((i + sub) / total, f"（{i + 1}/{total}）" + text)

        stopped_at: sqlite3.Row | None = None    # 停在哪个推主（还没处理）
        stop_reason = ""
        for i, user in enumerate(users):
            account, why = pool.pick()
            if account is None:
                stopped_at, stop_reason = user, why
                break
            try:
                client = clients.get(account["id"]) or factory.get_client(account)
                clients[account["id"]] = client
            except (XClientError, ValueError) as e:   # 凭据坏了 / 主号误配通道：这个号这次不用，换下一个
                stats.errors += 1
                stats.notes.insert(0, f"❌ 账号 @{account['handle']} 无法连接：{e}")
                _log_read(account["id"], _kind_of(account), "get_user_tweets", 0, success=False, error=str(e))
                pool.mark_rate_limited(account)
                account2, why2 = pool.pick()
                if account2 is None:
                    stopped_at, stop_reason = user, why2
                    break
                account = account2
                try:
                    client = clients.get(account["id"]) or factory.get_client(account)
                    clients[account["id"]] = client
                except (XClientError, ValueError) as e2:
                    stopped_at, stop_reason = user, f"账号 @{account['handle']} 也无法连接：{e2}"
                    break
            stats.users_polled += 1
            lookback_h = _row_int(user, "lookback_hours", 24)
            cursor = user["last_seen_tweet_id"]
            start_time = None if (cursor or not lookback_h) else datetime.now(timezone.utc) - timedelta(hours=lookback_h)
            try:
                gap = pool.wait_gap()
                _p(i, 0.05, f"@{user['handle']}：用 @{account['handle']} 从 X 拉取（{'游标之后的新推文' if cursor else f'最近 {lookback_h} 小时'}）…"
                   + (f"（间隔 {gap:.0f} 秒）" if gap else ""))
                pool.note_request(account)
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
            except RateLimited as e:
                # 撞 429：这个号暂停，本次运行到此为止，这位推主下次接着抓
                _log_read(account["id"], client.api_kind, "get_user_tweets", 0, success=False, error=str(e))
                until = pool.mark_rate_limited(account, getattr(e, "reset_at", None))
                stats.users_polled -= 1
                stopped_at = user
                stop_reason = f"@{account['handle']} 被 X 限流（429），该账号暂停到 {_hm(until)}"
                break
            except XClientError as e:
                stats.errors += 1
                stats.notes.insert(0, f"❌ @{user['handle']} 出错：{e}")
                _log_read(account["id"], client.api_kind, "get_user_tweets", 0, success=False, error=str(e))
            except Exception as e:  # 单推主隔离
                stats.errors += 1
                stats.notes.insert(0, f"❌ @{user['handle']} 出错：{type(e).__name__}: {e}")
                _log_read(account["id"], client.api_kind, "get_user_tweets", 0, success=False, error=str(e))

        if stopped_at is not None:
            resume_at = datetime.now(timezone.utc) + resume_delay()
            config.set_value(self.RESUME_FROM_KEY, str(stopped_at["id"]))
            config.set_value(self.RESUME_AT_KEY, to_iso(resume_at))
            left = total - stats.users_polled
            stats.paused = True
            stats.notes.insert(0, f"⏸ 本次运行暂停：{stop_reason}。还剩 {left} 位推主没抓（从 @{stopped_at['handle']} 起），"
                                  f"{_hm(resume_at)} 自动继续（设置 → 预算 → 「遇到限额后停多少分钟」）")
        else:
            self._clear_resume()
        if stats.tweets_fetched == 0 and stats.errors == 0 and stats.users_polled and stopped_at is None:
            stats.notes.append("所有推主都没有新推文（有游标的只看游标之后的，可在推主卡片上「重置游标」；首次抓取只看「首次回溯」小时数内的）")
        if pool.summary():
            stats.notes.append(pool.summary())
        if progress:
            progress(1.0, "完成" if stopped_at is None else "已暂停")
        return stats


def _kind_of(account: sqlite3.Row) -> str:
    return "x_official" if account["access_type"] == "official" else "x_unofficial"


def _hm(dt: datetime) -> str:
    from ..ui.layout import fmt_time  # 延迟导入：按界面显示时区
    return fmt_time(to_iso(dt))


def _log_read(account_id: int, api_kind: str, endpoint: str, reads: int,
              success: bool = True, error: str | None = None) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO action_log(account_id, api_kind, endpoint, reads_consumed, success, error, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (account_id, api_kind, endpoint, reads, 1 if success else 0, error, utcnow_iso()),
        )
        conn.commit()
