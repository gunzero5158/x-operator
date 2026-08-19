"""调度与启动（design-v1.1 §7.6）。

4 个后台 job（monitor / search / scheduled_check / dispatcher_tick），均整体 try/except
隔离。MVP 默认关闭自动轮询（app_settings.auto_jobs_enabled=0），改由 UI 手动按钮触发，
便于测试期精确控制；打开后按各自间隔自动运行。dispatcher_tick 始终运行（发送不耗读额度）。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler

from .. import config
from ..db.database import get_conn, parse_iso, utcnow_iso
from ..llm.client import LLMClient
from .compliance import ComplianceGuard
from .dispatcher import Dispatcher
from .matcher import MatchEngine
from .monitor import MonitorJob
from .schedule_calc import compute_next_run
from .search import SearchJob

log = logging.getLogger("x_operator.scheduler")


class Jobs:
    """把各 job 组装好，供 UI 手动触发与调度器共用同一份实例。"""

    def __init__(self):
        self.llm = LLMClient()
        self.match = MatchEngine(self.llm)
        self.monitor = MonitorJob(self.match)
        self.search = SearchJob(self.match, self.llm)
        self.guard = ComplianceGuard()
        self.dispatcher = Dispatcher(self.guard)

    def run_scheduled_posts(self) -> int:
        """扫描到点的定时发帖，生成审核队列条目（post 类）。返回生成条数。"""
        now = datetime.now(timezone.utc)
        generated = 0
        with get_conn() as conn:
            due = conn.execute(
                "SELECT sp.*, a.timezone AS acc_tz FROM scheduled_posts sp "
                "JOIN accounts a ON a.id = sp.account_id "
                "WHERE sp.status='active' AND sp.next_run_at IS NOT NULL AND sp.next_run_at<=?",
                (utcnow_iso(),)).fetchall()
        for sp in due:
            with get_conn() as conn:
                mat = conn.execute("SELECT * FROM materials WHERE id=?", (sp["material_id"],)).fetchone()
                if mat is None:
                    conn.execute("UPDATE scheduled_posts SET status='paused' WHERE id=?", (sp["id"],))
                    conn.commit()
                    continue
                status = "approved" if sp["auto_approve"] else "pending"
                conn.execute(
                    "INSERT INTO review_queue(account_id, action_type, material_id, scheduled_post_id, "
                    "final_text, auto_approve, status, created_at) VALUES (?,'post',?,?,?,?,?,?)",
                    (sp["account_id"], sp["material_id"], sp["id"], mat["text"],
                     sp["auto_approve"], status, utcnow_iso()))
                # 计算下次
                try:
                    nxt = compute_next_run(sp["schedule_type"], sp["schedule_expr"], now, sp["acc_tz"])
                except ValueError:
                    nxt = None
                if sp["schedule_type"] == "once" or nxt is None:
                    conn.execute("UPDATE scheduled_posts SET status='done', next_run_at=NULL, last_run_at=? WHERE id=?",
                                 (utcnow_iso(), sp["id"]))
                else:
                    conn.execute("UPDATE scheduled_posts SET next_run_at=?, last_run_at=? WHERE id=?",
                                 (nxt.strftime("%Y-%m-%dT%H:%M:%SZ"), utcnow_iso(), sp["id"]))
                conn.commit()
                generated += 1
        return generated


def _safe(fn):
    def wrapper():
        try:
            fn()
        except Exception:
            log.exception("后台 job 异常（已隔离）")
    return wrapper


def build_scheduler(jobs: Jobs) -> BackgroundScheduler:
    sched = BackgroundScheduler(timezone="UTC")

    @_safe
    def monitor_job():
        if config.get_bool("auto_jobs_enabled", False):
            jobs.monitor.run_once()

    @_safe
    def search_job():
        if config.get_bool("auto_jobs_enabled", False):
            jobs.search.run_once()

    @_safe
    def scheduled_check():
        jobs.run_scheduled_posts()

    @_safe
    def dispatcher_tick():
        jobs.dispatcher.tick()

    interval_min = max(1, config.get_int("monitor_interval_minutes", 50))
    sched.add_job(monitor_job, "interval", minutes=interval_min, id="monitor",
                  max_instances=1, coalesce=True, misfire_grace_time=60)
    sched.add_job(search_job, "interval", minutes=max(interval_min, 30), id="search",
                  max_instances=1, coalesce=True, misfire_grace_time=60)
    sched.add_job(scheduled_check, "interval", seconds=60, id="scheduled_check",
                  max_instances=1, coalesce=True, misfire_grace_time=60)
    sched.add_job(dispatcher_tick, "interval", seconds=60, id="dispatcher",
                  max_instances=1, coalesce=True, misfire_grace_time=60)
    return sched


def run_startup_recovery(jobs: Jobs) -> list[str]:
    """启动补扫（简化版）：过期扫描 + 残留 sending 回置 + 定时补算。"""
    msgs: list[str] = []
    now = utcnow_iso()
    with get_conn() as conn:
        # pending 且已过期 → expired
        cur = conn.execute(
            "UPDATE review_queue SET status='expired' WHERE status='pending' "
            "AND expires_at IS NOT NULL AND expires_at<?", (now,))
        if cur.rowcount:
            msgs.append(f"过期清理：{cur.rowcount} 条待审条目已标记过期")
        # 残留 sending（上次异常退出）→ 回置 approved
        cur = conn.execute("UPDATE review_queue SET status='approved' WHERE status='sending'")
        if cur.rowcount:
            msgs.append(f"恢复：{cur.rowcount} 条中断中的发送条目已回置待发")
        # 定时发帖 next_run_at 为空的 active 计划补算
        rows = conn.execute(
            "SELECT sp.*, a.timezone AS acc_tz FROM scheduled_posts sp JOIN accounts a ON a.id=sp.account_id "
            "WHERE sp.status='active' AND sp.next_run_at IS NULL").fetchall()
        conn.commit()
    from datetime import datetime as _dt
    for sp in rows:
        try:
            nxt = compute_next_run(sp["schedule_type"], sp["schedule_expr"],
                                   _dt.now(timezone.utc), sp["acc_tz"])
        except ValueError:
            nxt = None
        with get_conn() as conn:
            if nxt:
                conn.execute("UPDATE scheduled_posts SET next_run_at=? WHERE id=?",
                             (nxt.strftime("%Y-%m-%dT%H:%M:%SZ"), sp["id"]))
            else:
                conn.execute("UPDATE scheduled_posts SET status='done' WHERE id=?", (sp["id"],))
            conn.commit()
    if not msgs:
        msgs.append("启动检查完成，无需恢复")
    return msgs
