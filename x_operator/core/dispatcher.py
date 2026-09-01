"""Dispatcher（design-v1.1 §7.5）：从审核队列取 approved 条目，经合规校验后（模拟）发送。

每账号串行取最老 approved 条目；乐观锁置 sending；guard 硬违规→skipped，软违规→回置
approved；发送成功写 interactions + action_log、素材用量+1、设置下次可发时间。
去重账本唯一索引冲突→failed。
"""
from __future__ import annotations

import random
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from ..adapters import factory
from ..adapters.base import (AuthExpired, DuplicateContent, PermissionDenied,
                             RETRYABLE, TargetNotFound, XClientError)
from ..db.database import get_conn, utcnow_iso
from .compliance import ComplianceGuard


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
                report.notes.append(f"@{account['handle']}：{e}")
                continue
        return report

    def _dispatch_account(self, account: sqlite3.Row) -> tuple[bool, str]:
        now = datetime.now(timezone.utc)
        from ..db.database import parse_iso
        with get_conn() as conn:
            waiting = conn.execute(
                "SELECT COUNT(*) AS c FROM review_queue WHERE status='approved' AND account_id=?",
                (account["id"],)).fetchone()["c"]
        if waiting == 0:
            return False, ""  # 该账号没活儿，不算问题
        # 软预判：活跃时段 / next_allowed_at
        if not self.guard.is_in_active_hours(account, now):
            return False, (f"不在活跃时段（{account['active_hours_start']}-{account['active_hours_end']} "
                           f"{account['timezone']}），{waiting} 条待发到时段内自动发送")
        na = parse_iso(account["next_allowed_at"])
        if na and now < na:
            secs = int((na - now).total_seconds())
            return False, f"距上次发送的随机间隔未到，还需约 {secs} 秒（{waiting} 条待发）"

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

    def send_item(self, account: sqlite3.Row, item: sqlite3.Row) -> bool:
        try:
            client = factory.get_client(account)
        except (XClientError, ValueError) as e:
            # 凭据缺失/主号误配非官方：回置 approved，等用户补凭据后自动继续
            self._set_status(item["id"], "approved", error_msg=str(e))
            return False
        try:
            if item["action_type"] == "reply":
                with get_conn() as conn:
                    tgt = conn.execute("SELECT * FROM target_tweets WHERE id=?",
                                       (item["target_tweet_id"],)).fetchone()
                res = client.reply(item["final_text"], tgt["tweet_id"])
                target_tweet_id_str = tgt["tweet_id"]
                author_id = tgt["author_id"]
            else:
                res = client.post(item["final_text"])
                target_tweet_id_str = res.tweet_id
                author_id = None
        except (AuthExpired,) as e:
            self._set_account_auth_error(account["id"])
            self._set_status(item["id"], "failed", error_msg=str(e))
            return False
        except (DuplicateContent, PermissionDenied, TargetNotFound) as e:
            self._set_status(item["id"], "failed", error_msg=str(e))
            return False
        except RETRYABLE as e:
            self._retry_or_fail(item, str(e))
            return False
        except XClientError as e:
            self._set_status(item["id"], "failed", error_msg=str(e))
            return False

        # 成功：写 interactions + 置 sent（同事务）
        has_link = 1 if ("http://" in item["final_text"] or "https://" in item["final_text"]) else 0
        try:
            with get_conn() as conn:
                conn.execute(
                    "INSERT INTO interactions(account_id, action, tweet_id, author_id, sent_at) VALUES (?,?,?,?,?)",
                    (account["id"], item["action_type"], target_tweet_id_str, author_id, utcnow_iso()))
                conn.execute(
                    "UPDATE review_queue SET status='sent', sent_tweet_id=?, sent_at=? WHERE id=?",
                    (res.tweet_id, utcnow_iso(), item["id"]))
                if item["material_id"]:
                    conn.execute(
                        "UPDATE materials SET usage_count=usage_count+1, last_used_at=? WHERE id=?",
                        (utcnow_iso(), item["material_id"]))
                # 下次可发时间
                lo, hi = account["min_interval_sec"], account["max_interval_sec"]
                delay = random.randint(lo, hi) if hi >= lo else lo
                next_at = (datetime.now(timezone.utc) + timedelta(seconds=delay)).strftime("%Y-%m-%dT%H:%M:%SZ")
                conn.execute("UPDATE accounts SET next_allowed_at=? WHERE id=?", (next_at, account["id"]))
                conn.execute(
                    "INSERT INTO action_log(account_id, api_kind, endpoint, has_link, success, created_at) "
                    "VALUES (?,?,?,?,1,?)",
                    (account["id"], client.api_kind, item["action_type"], has_link, utcnow_iso()))
                conn.commit()
        except sqlite3.IntegrityError:
            # 去重账本冲突（竞态）
            self._set_status(item["id"], "failed", error_msg="去重账本冲突：该推文已被回复")
            return False
        # 发送后核实：X 有时返回成功但推文被静默丢弃/限制，这里立刻回查一次
        self.verify_item(item["id"], client=client)
        self.last_sent_url = f"https://x.com/{account['handle']}/status/{res.tweet_id}"
        return True

    last_sent_url: str | None = None

    def verify_item(self, item_id: int, client=None) -> str:
        """回查已发条目在 X 上是否真的存在。返回 verify_status：ok / missing / unknown。"""
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
        except Exception:
            exists = None
        status = "ok" if exists else ("missing" if exists is False else "unknown")
        with get_conn() as conn:
            conn.execute("UPDATE review_queue SET verify_status=? WHERE id=?", (status, item_id))
            conn.commit()
        return status

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
