"""Dispatcher（design-v1.1 §7.5）：从审核队列取 approved 条目，经合规校验后发送。

每账号一把锁 + 串行取最老 approved 条目（UI「触发发送」和后台 60 秒 tick 可能同时来，
锁保证同一账号的「限速检查 → 发送 → 记账」不会交错，日上限/间隔才真的有效）；
乐观锁置 sending；guard 硬违规→skipped，软违规→回置 approved。

发送成功后的落库分两步：先把「已发出 + X 上的 id」写死（带重试），再做账本/素材用量/间隔等记账——
推文已经在 X 上了，后面任何一步失败都不能让它退回「待发送」被再发一次。
"""
from __future__ import annotations

import logging
import random
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from ..adapters import factory
from ..adapters.base import (AuthExpired, DuplicateContent, NetworkError, PermissionDenied,
                             PostResult, RateLimited, TargetNotFound, XClientError)
from ..db.database import get_conn, parse_iso, to_iso, utcnow_iso
from .compliance import ComplianceGuard

log = logging.getLogger("x_operator.dispatcher")

RATE_LIMIT_DEFAULT_WAIT = timedelta(minutes=15)
# 发送超时后到自己时间线上「找回」刚发的那条：只认这个时间范围内的
JUST_SENT_WINDOW = timedelta(minutes=15)


@dataclass
class DispatchReport:
    sent: int = 0
    notes: list[str] = field(default_factory=list)   # 每个账号为何没发（中文）

    def as_msg(self, mock: bool = False) -> str:
        head = f"本轮发送 {self.sent} 条" + ("（Mock 模拟）" if mock else "")
        if self.notes:
            head += "。" + "；".join(self.notes[:4])
        return head


class Dispatcher:
    def __init__(self, guard: ComplianceGuard):
        self.guard = guard
        self.last_sent_url: str | None = None
        self._locks: dict[int, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _account_lock(self, account_id: int) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(account_id, threading.Lock())

    def tick(self) -> DispatchReport:
        """每个活跃账号最多发 1 条；返回发送数与各账号未发原因。"""
        report = DispatchReport()
        with get_conn() as conn:
            accounts = conn.execute("SELECT * FROM accounts WHERE status='active'").fetchall()
            approved_total = conn.execute(
                "SELECT COUNT(*) AS c FROM review_queue WHERE status='approved'").fetchone()["c"]
        if not accounts:
            report.notes.append("没有状态为「启用」的账号")
            return report
        if approved_total == 0:
            report.notes.append("审核队列里没有「待发送」（已批准）的条目")
            return report
        for account in accounts:
            try:
                ok, why = self._dispatch_account(account)
                if ok:
                    report.sent += 1
                if why:
                    report.notes.append(f"@{account['handle']}：{why}")
            except Exception as e:  # 账号间隔离
                log.exception("账号 @%s 分发异常", account["handle"])
                report.notes.append(f"@{account['handle']}：{e}")
                continue
        return report

    def _dispatch_account(self, account: sqlite3.Row) -> tuple[bool, str]:
        with get_conn() as conn:
            waiting = conn.execute(
                "SELECT COUNT(*) AS c FROM review_queue WHERE status='approved' AND account_id=?",
                (account["id"],)).fetchone()["c"]
        if waiting == 0:
            return False, ""  # 该账号没活儿，不算问题
        lock = self._account_lock(account["id"])
        if not lock.acquire(blocking=False):
            return False, "该账号正有另一次发送在进行中，本轮跳过"
        try:
            return self._dispatch_account_locked(account, waiting)
        finally:
            lock.release()

    def _dispatch_account_locked(self, account: sqlite3.Row, waiting: int) -> tuple[bool, str]:
        now = datetime.now(timezone.utc)
        # 软预判：活跃时段 / next_allowed_at（锁内重新读账号，拿到最新的 next_allowed_at）
        with get_conn() as conn:
            account = conn.execute("SELECT * FROM accounts WHERE id=?", (account["id"],)).fetchone()
        if not self.guard.is_in_active_hours(account, now):
            return False, (f"不在活跃时段（{account['active_hours_start']}-{account['active_hours_end']} "
                           f"{account['timezone']}），{waiting} 条待发到时段内自动发送")
        na = parse_iso(account["next_allowed_at"])
        if na and now < na:
            secs = int((na - now).total_seconds())
            return False, f"距下次可发时间还有约 {secs} 秒（{waiting} 条待发）"

        with get_conn() as conn:
            item = conn.execute(
                "SELECT * FROM review_queue WHERE status='approved' AND account_id=? "
                "ORDER BY created_at ASC LIMIT 1", (account["id"],)).fetchone()
            if item is None:
                return False, ""
            # 乐观锁
            cur = conn.execute("UPDATE review_queue SET status='sending' WHERE id=? AND status='approved'",
                               (item["id"],))
            conn.commit()
            if cur.rowcount == 0:
                return False, ""
            item = conn.execute("SELECT * FROM review_queue WHERE id=?", (item["id"],)).fetchone()

        # 合规最终校验
        gr = self.guard.check(account, item, now)
        if not gr.ok:
            if gr.hard:
                self._set_status(item["id"], "skipped", skip_reason=gr.code.value if gr.code else "guard")
                return False, f"条目 #{item['id']} 被合规拦截并跳过：{gr.detail}"
            self._set_status(item["id"], "approved")  # 软违规回置，下轮再试
            return False, gr.detail

        if self.send_item(account, item):
            with get_conn() as conn:
                vs = conn.execute("SELECT verify_status FROM review_queue WHERE id=?", (item["id"],)).fetchone()["verify_status"]
            tail = {"ok": "，已回查确认存在 ✅", "missing": "，⚠ 但回查时在 X 上查不到（可能被限制/静默丢弃）",
                    }.get(vs, "")
            return True, f"条目 #{item['id']} 已发出：{self.last_sent_url}{tail}"
        with get_conn() as conn:
            row = conn.execute("SELECT status, error_msg FROM review_queue WHERE id=?", (item["id"],)).fetchone()
        if row["status"] == "approved":
            return False, f"条目 #{item['id']} 未发出、已回置待发：{row['error_msg'] or '稍后重试'}"
        return False, f"条目 #{item['id']} 发送失败（{row['status']}）：{row['error_msg'] or '未知错误'}"

    # ------------------------------------------------------------------ 发送
    def send_item(self, account: sqlite3.Row, item: sqlite3.Row) -> bool:
        try:
            client = factory.get_client(account)
        except (XClientError, ValueError) as e:
            # 凭据缺失/主号误配非官方：回置 approved，等用户补凭据后自动继续
            self._set_status(item["id"], "approved", error_msg=str(e))
            return False
        tgt = None
        if item["action_type"] == "reply":
            with get_conn() as conn:
                tgt = conn.execute("SELECT * FROM target_tweets WHERE id=?", (item["target_tweet_id"],)).fetchone()
            if tgt is None:
                self._set_status(item["id"], "failed", error_msg="目标推文记录已不存在")
                return False
        try:
            if tgt is not None:
                res = client.reply(item["final_text"], tgt["tweet_id"])
            else:
                res = client.post(item["final_text"])
        except AuthExpired as e:
            self._set_account_auth_error(account["id"])
            self._set_status(item["id"], "failed", error_msg=str(e))
            return False
        except (DuplicateContent, PermissionDenied, TargetNotFound) as e:
            self._set_status(item["id"], "failed", error_msg=str(e))
            return False
        except RateLimited as e:
            self._defer_for_rate_limit(account, item, e)
            return False
        except NetworkError as e:
            # 超时/断网：请求可能其实已经到了 X。先到自己时间线上找一下，找到了就按发出处理，绝不盲目重发
            res = self._find_just_sent(client, account, item, tgt)
            if res is None:
                self._retry_or_fail(item, str(e))
                return False
            log.warning("条目 #%s 发送时报网络错误，但在时间线上找到了已发出的推文 %s，按成功处理", item["id"], res.tweet_id)
        except XClientError as e:
            self._set_status(item["id"], "failed", error_msg=str(e))
            return False

        self._record_sent(account, item, client, res, tgt)
        # 发送后核实：X 有时返回成功但推文被静默丢弃/限制，这里立刻回查一次
        self.verify_item(item["id"], client=client)
        self.last_sent_url = f"https://x.com/{account['handle']}/status/{res.tweet_id}"
        return True

    def _record_sent(self, account: sqlite3.Row, item: sqlite3.Row, client, res: PostResult,
                     tgt: sqlite3.Row | None) -> None:
        sent_at = utcnow_iso()
        # 第一步（最关键）：推文已经在 X 上了，必须把「已发出」写死；库忙就重试，绝不让它退回待发送
        self._write_with_retry(
            lambda conn: conn.execute(
                "UPDATE review_queue SET status='sent', sent_tweet_id=?, sent_at=?, error_msg=NULL WHERE id=?",
                (res.tweet_id, sent_at, item["id"])),
            what=f"条目 #{item['id']} 标记已发出（X id {res.tweet_id}）")
        # 第二步：账本 / 素材用量 / 下次可发时间 / 日志。任何一步失败只记日志，不影响已发出状态
        has_link = 1 if ("http://" in item["final_text"] or "https://" in item["final_text"]) else 0
        ledger_tweet_id = tgt["tweet_id"] if tgt is not None else res.tweet_id
        author_id = tgt["author_id"] if tgt is not None else None

        def _bookkeeping(conn: sqlite3.Connection) -> None:
            try:
                conn.execute(
                    "INSERT INTO interactions(account_id, action, tweet_id, author_id, sent_at) VALUES (?,?,?,?,?)",
                    (account["id"], item["action_type"], ledger_tweet_id, author_id, sent_at))
            except sqlite3.IntegrityError:
                # 去重账本里已有（另一账号刚回过同一条）：本条已经发出了，只能记下来给人看
                conn.execute("UPDATE review_queue SET error_msg=? WHERE id=?",
                             ("注意：去重账本里已有这条推文（另一账号也回复过），本条已发出，请自行核对", item["id"]))
            if item["material_id"]:
                conn.execute("UPDATE materials SET usage_count=usage_count+1, last_used_at=? WHERE id=?",
                             (sent_at, item["material_id"]))
            lo, hi = account["min_interval_sec"], account["max_interval_sec"]
            delay = random.randint(lo, hi) if hi >= lo else lo
            next_at = to_iso(datetime.now(timezone.utc) + timedelta(seconds=delay))
            conn.execute("UPDATE accounts SET next_allowed_at=? WHERE id=?", (next_at, account["id"]))
            conn.execute(
                "INSERT INTO action_log(account_id, api_kind, endpoint, has_link, success, created_at) "
                "VALUES (?,?,?,?,1,?)",
                (account["id"], client.api_kind, item["action_type"], has_link, sent_at))

        try:
            self._write_with_retry(_bookkeeping, what=f"条目 #{item['id']} 发送后记账")
        except Exception:
            log.exception("条目 #%s 发送后记账失败（推文已发出，状态已标记 sent）", item["id"])

    @staticmethod
    def _write_with_retry(fn, what: str, attempts: int = 5) -> None:
        for i in range(attempts):
            try:
                with get_conn() as conn:
                    fn(conn)
                    conn.commit()
                return
            except sqlite3.OperationalError as e:  # database is locked 等
                if i == attempts - 1:
                    log.critical("%s：数据库写入连续失败（%s）", what, e)
                    raise
                time.sleep(0.3 * (i + 1))

    def _find_just_sent(self, client, account: sqlite3.Row, item: sqlite3.Row,
                        tgt: sqlite3.Row | None) -> PostResult | None:
        """发送请求超时后，到自己时间线上找刚才那条：回复看「回的是不是同一条推文」，发帖看正文是否一致。"""
        from .monitor import _log_read
        try:
            me = client.get_me()
            fr = client.get_user_tweets(me.user_id, max_results=10, include_replies=True)
            _log_read(account["id"], client.api_kind, "get_user_tweets", fr.reads_consumed)
        except Exception as e:
            log.warning("超时后找回刚发的推文失败：%s", e)
            return None
        cutoff = datetime.now(timezone.utc) - JUST_SENT_WINDOW
        want = _text_key(item["final_text"])
        for t in fr.tweets:
            if t.created_at < cutoff:
                continue
            if tgt is not None:
                if t.in_reply_to_tweet_id == tgt["tweet_id"]:
                    return PostResult(tweet_id=t.tweet_id)
            elif _text_key(t.text) == want:
                return PostResult(tweet_id=t.tweet_id)
        return None

    def _defer_for_rate_limit(self, account: sqlite3.Row, item: sqlite3.Row, e: RateLimited) -> None:
        """429：不算重试次数，把账号的下次可发时间推到 X 给的重置时间（没给就 15 分钟）。"""
        now = datetime.now(timezone.utc)
        reset = e.reset_at if (e.reset_at and e.reset_at > now) else now + RATE_LIMIT_DEFAULT_WAIT
        with get_conn() as conn:
            conn.execute("UPDATE review_queue SET status='approved', error_msg=? WHERE id=?",
                         (f"X 限流，暂停到 {to_iso(reset)} 后自动继续", item["id"]))
            cur = parse_iso(conn.execute("SELECT next_allowed_at FROM accounts WHERE id=?",
                                         (account["id"],)).fetchone()["next_allowed_at"])
            if cur is None or cur < reset:
                conn.execute("UPDATE accounts SET next_allowed_at=? WHERE id=?", (to_iso(reset), account["id"]))
            conn.commit()

    # ------------------------------------------------------------------ 回查
    def verify_item(self, item_id: int, client=None) -> str:
        """回查已发条目在 X 上是否真的存在。返回 verify_status：ok / missing / unknown。"""
        from .monitor import _log_read
        with get_conn() as conn:
            item = conn.execute("SELECT rq.*, a.handle FROM review_queue rq JOIN accounts a ON a.id=rq.account_id "
                                "WHERE rq.id=?", (item_id,)).fetchone()
        if item is None or item["status"] != "sent" or not item["sent_tweet_id"]:
            return "unknown"
        try:
            if client is None:
                with get_conn() as conn:
                    acc = conn.execute("SELECT * FROM accounts WHERE id=?", (item["account_id"],)).fetchone()
                client = factory.get_client(acc)
            exists = client.tweet_exists(item["sent_tweet_id"])
            _log_read(item["account_id"], client.api_kind, "tweet_exists", 1)
        except Exception:
            exists = None
        status = "ok" if exists else ("missing" if exists is False else "unknown")
        with get_conn() as conn:
            conn.execute("UPDATE review_queue SET verify_status=? WHERE id=?", (status, item_id))
            conn.commit()
        return status

    # ------------------------------------------------------------------ 状态
    def _set_status(self, item_id: int, status: str, skip_reason: str | None = None,
                    error_msg: str | None = None) -> None:
        with get_conn() as conn:
            conn.execute(
                "UPDATE review_queue SET status=?, skip_reason=COALESCE(?,skip_reason), "
                "error_msg=COALESCE(?,error_msg), decided_at=? WHERE id=?",
                (status, skip_reason, error_msg, utcnow_iso(), item_id))
            conn.commit()

    def _retry_or_fail(self, item: sqlite3.Row, err: str) -> None:
        with get_conn() as conn:
            if item["retry_count"] < 2:
                conn.execute("UPDATE review_queue SET status='approved', retry_count=retry_count+1, error_msg=? WHERE id=?",
                             (err, item["id"]))
            else:
                conn.execute("UPDATE review_queue SET status='failed', error_msg=? WHERE id=?",
                             (err, item["id"]))
            conn.commit()

    def _set_account_auth_error(self, account_id: int) -> None:
        with get_conn() as conn:
            conn.execute("UPDATE accounts SET status='auth_error' WHERE id=?", (account_id,))
            conn.commit()
        factory.invalidate(account_id)


_URL_RE = re.compile(r"https?://\S+")


def _text_key(s: str) -> str:
    """比较「是不是同一条正文」：X 会把链接改写成 t.co，所以去掉链接再比。"""
    return " ".join(_URL_RE.sub("", s or "").split())
