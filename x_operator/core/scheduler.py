"""调度与启动（design-v1.1 §7.6）。

后台 job（monitor / search / scheduled_check / dispatcher_tick），均整体 try/except 隔离。
自动搜索/自动监控受总开关 auto_jobs_enabled（默认关）+ 各自的单独开关控制，节奏可选「每隔 N 分钟」或
「每天固定时间点」（AUTO_JOBS / build_trigger）；发送分发有自己的开关（默认开）；定时计划检查始终运行。
scheduled_check 顺带做「过期清扫」：待审核超时的条目标过期，对应抓取记录也标过期。
"""
from __future__ import annotations

import logging
import re
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.combining import OrTrigger
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

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
        self.scheduler: BackgroundScheduler | None = None   # build_scheduler 里赋值，设置页改节奏时用
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


# ---------------------------------------------------------------------------------
# 自动轮询的节奏：每个 job 可选「每隔 N 分钟」或「每天固定时间点」，按设置里的时区算；改设置后立即重排，不用重启
# ---------------------------------------------------------------------------------
AUTO_JOBS = {
    # id: (名称, 说明, 单独开关键, 默认间隔分钟, 默认固定时间)
    "search":  ("自动搜索", "把所有启用的搜索规则各跑一次（受总开关 + 读额度熔断）", "search_auto_enabled", 720, "08:00, 20:00"),
    "monitor": ("自动监控", "把所有启用的监控推主各拉一次（受总开关 + 读额度熔断）", "monitor_auto_enabled", 50, "09:00, 13:00, 18:00"),
}
ALWAYS_JOBS = {
    "dispatcher": ("发送分发", "每分钟检查一次「待发送」条目，按各账号的间隔/日上限/活跃时段发出。关掉后只能手动点「触发发送」", "dispatch_auto_enabled"),
    "scheduled_check": ("定时计划 + 过期清扫", "每分钟检查到点的定时发帖计划，生成到审核队列；顺带把超时的待审核条目标过期。始终开启", None),
}


def parse_daily_times(raw: str) -> list[tuple[int, int]]:
    """"08:00, 20:30" → [(8,0),(20,30)]；写错的项跳过；去重排序。"""
    out = set()
    for part in re.split(r"[,，、;\s]+", raw or ""):
        m = re.fullmatch(r"(\d{1,2}):(\d{2})", part.strip())
        if m and 0 <= int(m.group(1)) < 24 and 0 <= int(m.group(2)) < 60:
            out.add((int(m.group(1)), int(m.group(2))))
    return sorted(out)


def auto_timezone() -> str:
    tz = (config.get("auto_jobs_timezone") or "Asia/Shanghai").strip()
    try:
        ZoneInfo(tz)
        return tz
    except Exception:
        return "Asia/Shanghai"


def build_trigger(job: str):
    name, _desc, _key, default_min, default_times = AUTO_JOBS[job]
    mode = (config.get(f"{job}_schedule_mode") or "interval").strip()
    if mode == "daily":
        times = parse_daily_times(config.get(f"{job}_daily_times") or default_times)
        if times:
            tz = auto_timezone()
            crons = [CronTrigger(hour=h, minute=m, timezone=tz) for h, m in times]
            return crons[0] if len(crons) == 1 else OrTrigger(crons)
    minutes = max(5, config.get_int(f"{job}_interval_minutes", default_min))
    return IntervalTrigger(minutes=minutes)


def describe_schedule(job: str) -> str:
    _name, _desc, _key, default_min, default_times = AUTO_JOBS[job]
    mode = (config.get(f"{job}_schedule_mode") or "interval").strip()
    if mode == "daily":
        times = parse_daily_times(config.get(f"{job}_daily_times") or default_times)
        if times:
            return "每天 " + "、".join(f"{h:02d}:{m:02d}" for h, m in times) + f"（{auto_timezone()}）"
        return "每天固定时间（没填有效时间，暂按间隔跑）"
    minutes = max(5, config.get_int(f"{job}_interval_minutes", default_min))
    return f"每隔 {minutes} 分钟（从程序启动/改设置那一刻起算）"


def job_enabled(job: str) -> bool:
    if job in AUTO_JOBS:
        return config.get_bool("auto_jobs_enabled", False) and config.get_bool(AUTO_JOBS[job][2], True)
    key = ALWAYS_JOBS[job][2]
    return True if key is None else config.get_bool(key, True)


def reschedule_auto_jobs(sched: BackgroundScheduler | None) -> None:
    if sched is None:
        return
    for job in AUTO_JOBS:
        sched.reschedule_job(job, trigger=build_trigger(job))


def next_runs(sched: BackgroundScheduler | None) -> dict[str, datetime | None]:
    """各 job 的下次运行时间（已转成设置里的时区）；未启用的返回 None。"""
    out: dict[str, datetime | None] = {}
    tz = ZoneInfo(auto_timezone())
    for job in list(AUTO_JOBS) + list(ALWAYS_JOBS):
        nxt = None
        if sched is not None and job_enabled(job):
            j = sched.get_job(job)
            if j is not None and j.next_run_time:
                nxt = j.next_run_time.astimezone(tz)
        out[job] = nxt
    return out


def build_scheduler(jobs: Jobs) -> BackgroundScheduler:
    sched = BackgroundScheduler(timezone="UTC")

    @_safe
    def monitor_job():
        if job_enabled("monitor"):
            st = jobs.monitor.run_once(auto=True)
            log.info("自动监控：%s", st.as_msg())

    @_safe
    def search_job():
        if job_enabled("search"):
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
        if job_enabled("dispatcher"):
            jobs.dispatcher.tick()

    sched.add_job(monitor_job, build_trigger("monitor"), id="monitor",
                  max_instances=1, coalesce=True, misfire_grace_time=300)
    sched.add_job(search_job, build_trigger("search"), id="search",
                  max_instances=1, coalesce=True, misfire_grace_time=300)
    sched.add_job(scheduled_check, "interval", seconds=60, id="scheduled_check",
                  max_instances=1, coalesce=True, misfire_grace_time=60)
    sched.add_job(dispatcher_tick, "interval", seconds=60, id="dispatcher",
                  max_instances=1, coalesce=True, misfire_grace_time=60)
    jobs.scheduler = sched
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
