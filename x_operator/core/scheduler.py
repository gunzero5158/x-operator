"""调度与启动（design-v1.1 §7.6）。

后台 job（monitor / search / scheduled_check / dispatcher_tick），均整体 try/except 隔离。
默认关闭自动轮询（app_settings.auto_jobs_enabled=0），改由 UI 手动按钮触发，便于测试期精确控制；
打开后按各自间隔自动运行。scheduled_check 与 dispatcher_tick 始终运行（发送不耗读额度）。
scheduled_check 顺带做「过期清扫」：待审核超时的条目标过期，对应抓取记录也标过期。
"""
from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.background import BackgroundScheduler

from .. import config
from ..db.database import get_conn, parse_iso, to_iso, utcnow_iso
from ..llm.client import LLMClient
from .compliance import ComplianceGuard
from .dispatcher import Dispatcher
from .matcher import MatchEngine
from .monitor import MonitorJob
from .schedule_calc import compute_next_run
from .search import SearchJob

log = logging.getLogger("x_operator.scheduler")


def enqueue_scheduled_post(conn: sqlite3.Connection, sp: sqlite3.Row) -> bool:
    """按定时计划生成一条「发帖」队列条目。素材不在了返回 False（不发空内容）。不提交。"""
    mat = conn.execute("SELECT * FROM materials WHERE id=? AND deleted_at IS NULL", (sp["material_id"],)).fetchone()
    if mat is None:
        return False
    status = "approved" if sp["auto_approve"] else "pending"
    conn.execute(
        "INSERT INTO review_queue(account_id, action_type, material_id, scheduled_post_id, "
        "final_text, auto_approve, status, origin, created_at) VALUES (?,'post',?,?,?,?,?,'scheduled',?)",
        (sp["account_id"], sp["material_id"], sp["id"], mat["text"], sp["auto_approve"], status, utcnow_iso()))
    return True


class Jobs:
    """把各 job 组装好，供 UI 手动触发与调度器共用同一份实例。"""

    def __init__(self):
        self.llm = LLMClient()
        self.match = MatchEngine(self.llm)
        self.monitor = MonitorJob(self.match)
        self.search = SearchJob(self.match, self.llm)
        self.guard = ComplianceGuard()
        self.dispatcher = Dispatcher(self.guard)
        self._sched_lock = threading.Lock()

    def run_scheduled_posts(self) -> int:
        """扫描到点的定时发帖，生成审核队列条目（post 类）。返回生成条数。

        UI 按钮和后台 60 秒 job 可能同时来：进程内加锁，再用「next_run_at 没被别人改过」做原子认领，
        同一个到点只会生成一条。"""
        now = datetime.now(timezone.utc)
        generated = 0
        with self._sched_lock:
            with get_conn() as conn:
                due = conn.execute(
                    "SELECT sp.*, a.timezone AS acc_tz FROM scheduled_posts sp "
                    "JOIN accounts a ON a.id = sp.account_id "
                    "WHERE sp.status='active' AND sp.next_run_at IS NOT NULL AND sp.next_run_at<=?",
                    (utcnow_iso(),)).fetchall()
            for sp in due:
                try:
                    nxt = compute_next_run(sp["schedule_type"], sp["schedule_expr"], now, sp["acc_tz"])
                except ValueError:
                    nxt = None
                with get_conn() as conn:
                    # 先认领（只有 next_run_at 还是我们读到的那个值时才成功），认领到了才生成
                    if sp["schedule_type"] == "once" or nxt is None:
                        cur = conn.execute(
                            "UPDATE scheduled_posts SET status='done', next_run_at=NULL, last_run_at=? "
                            "WHERE id=? AND status='active' AND next_run_at=?",
                            (utcnow_iso(), sp["id"], sp["next_run_at"]))
                    else:
                        cur = conn.execute(
                            "UPDATE scheduled_posts SET next_run_at=?, last_run_at=? "
                            "WHERE id=? AND status='active' AND next_run_at=?",
                            (to_iso(nxt), utcnow_iso(), sp["id"], sp["next_run_at"]))
                    if cur.rowcount == 0:
                        conn.rollback()
                        continue
                    if not enqueue_scheduled_post(conn, sp):
                        # 素材被删/进回收站 → 计划暂停，不发空内容
                        conn.execute("UPDATE scheduled_posts SET status='paused' WHERE id=?", (sp["id"],))
                        conn.commit()
                        continue
                    conn.commit()
                    generated += 1
        return generated


def expire_stale() -> int:
    """待审核超过时效的条目 → 过期；它们的抓取记录也从「已进审核队列」改成「已过期」。返回过期条数。"""
    now = utcnow_iso()
    with get_conn() as conn:
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM review_queue WHERE status='pending' AND expires_at IS NOT NULL AND expires_at<?", (now,))]
        if not ids:
            return 0
        marks = ",".join("?" * len(ids))
        conn.execute(f"UPDATE review_queue SET status='expired', decided_at=? WHERE id IN ({marks})", [now, *ids])
        conn.execute(
            "UPDATE target_tweets SET process_status='expired', "
            "llm_relevance_reason='待审核超时未处理，已过期（可点「选素材」「AI 撰写」重新生成）' "
            f"WHERE process_status='queued' AND id IN (SELECT target_tweet_id FROM review_queue WHERE id IN ({marks}))", ids)
        conn.commit()
    return len(ids)


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
            st = jobs.monitor.run_once(auto=True)
            log.info("自动监控：%s", st.as_msg())

    @_safe
    def search_job():
        if config.get_bool("auto_jobs_enabled", False):
            st = jobs.search.run_once(auto=True)
            log.info("自动搜索：%s", st.as_msg())

    @_safe
    def scheduled_check():
        n = expire_stale()
        if n:
            log.info("过期清扫：%d 条待审核条目已过期", n)
        jobs.run_scheduled_posts()

    @_safe
    def dispatcher_tick():
        jobs.dispatcher.tick()

    interval_min = max(1, config.get_int("monitor_interval_minutes", 50))
    runs_per_day = max(1, config.get_int("search_runs_per_day", 2))
    search_min = max(30, 1440 // runs_per_day)
    sched.add_job(monitor_job, "interval", minutes=interval_min, id="monitor",
                  max_instances=1, coalesce=True, misfire_grace_time=60)
    sched.add_job(search_job, "interval", minutes=search_min, id="search",
                  max_instances=1, coalesce=True, misfire_grace_time=60)
    sched.add_job(scheduled_check, "interval", seconds=60, id="scheduled_check",
                  max_instances=1, coalesce=True, misfire_grace_time=60)
    sched.add_job(dispatcher_tick, "interval", seconds=60, id="dispatcher",
                  max_instances=1, coalesce=True, misfire_grace_time=60)
    return sched


def run_startup_recovery(jobs: Jobs) -> list[str]:
    """启动补扫：过期清扫 + 残留 sending 处理 + 定时计划补算/错过判定。"""
    msgs: list[str] = []
    now_dt = datetime.now(timezone.utc)
    n = expire_stale()
    if n:
        msgs.append(f"过期清理：{n} 条待审条目已标记过期")
    with get_conn() as conn:
        # 残留 sending（上次异常退出）：已经拿到 X 的 id 的 → 其实发出去了，标 sent；
        # 没拿到 id 的不能重发（不知道有没有发出去），标失败并提示人工到 X 上确认
        cur = conn.execute("UPDATE review_queue SET status='sent' WHERE status='sending' AND sent_tweet_id IS NOT NULL")
        if cur.rowcount:
            msgs.append(f"恢复：{cur.rowcount} 条发送中断的条目已确认发出（有 X 的推文 id）")
        cur = conn.execute(
            "UPDATE review_queue SET status='failed', decided_at=?, "
            "error_msg='程序中断时正在发送，无法确认是否已发出。请到 X 上看一下：没发出的话在抓取记录里重新生成' "
            "WHERE status='sending'", (utcnow_iso(),))
        if cur.rowcount:
            msgs.append(f"恢复：{cur.rowcount} 条发送中断、结果未知的条目已标记失败（请到 X 上确认，勿盲目重发）")
        # 定时计划：next_run_at 为空的 active 计划补算；错过太久（超过宽限）的按错过处理
        grace_h = max(0, config.get_int("grace_period_hours", 2))
        rows = conn.execute(
            "SELECT sp.*, a.timezone AS acc_tz FROM scheduled_posts sp JOIN accounts a ON a.id=sp.account_id "
            "WHERE sp.status='active' AND (sp.next_run_at IS NULL OR sp.next_run_at<?)",
            (to_iso(now_dt - timedelta(hours=grace_h)),)).fetchall()
        conn.commit()
    missed = 0
    for sp in rows:
        try:
            nxt = compute_next_run(sp["schedule_type"], sp["schedule_expr"], now_dt, sp["acc_tz"])
        except ValueError:
            nxt = None
        with get_conn() as conn:
            if sp["next_run_at"] is not None:
                missed += 1
            if sp["schedule_type"] == "once" or nxt is None:
                conn.execute("UPDATE scheduled_posts SET status=?, next_run_at=NULL WHERE id=?",
                             ("missed" if sp["next_run_at"] is not None else "done", sp["id"]))
            else:
                conn.execute("UPDATE scheduled_posts SET next_run_at=? WHERE id=?", (to_iso(nxt), sp["id"]))
            conn.commit()
    if missed:
        msgs.append(f"定时计划：{missed} 个到点超过 {grace_h} 小时宽限的计划按「错过」处理（一次性计划标为已错过，周期计划跳到下一次）")
    if not msgs:
        msgs.append("启动检查完成，无需恢复")
    return msgs
