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
from ..llm.client import LLMClient, LLMError
from . import media
from .compliance import ComplianceGuard
from .dispatcher import Dispatcher
from .matcher import MatchEngine
from .monitor import MonitorJob
from .schedule_calc import compute_next_run
from .search import SearchJob

log = logging.getLogger("x_operator.scheduler")


POST_MODE_LABEL = {"fixed": "固定一条素材", "pool": "素材池轮流", "ai_topic": "AI 按主题创作"}
RECENT_POST_DAYS = 30


def recent_post_texts(conn: sqlite3.Connection, account_id: int, limit: int = 20) -> list[str]:
    """这个账号最近发过 / 排着要发的推文正文——用来避开重复。"""
    cutoff = to_iso(datetime.now(timezone.utc) - timedelta(days=RECENT_POST_DAYS))
    rows = conn.execute(
        "SELECT final_text FROM review_queue WHERE account_id=? AND action_type='post' "
        "AND status IN ('sent','approved','pending','sending') AND created_at>=? ORDER BY created_at DESC LIMIT ?",
        (account_id, cutoff, limit)).fetchall()
    return [r["final_text"] for r in rows]


def _sp_get(sp: sqlite3.Row, key: str, default):
    try:
        v = sp[key]
    except (IndexError, KeyError):
        return default
    return default if v is None else v


def pick_pool_material(conn: sqlite3.Connection, sp: sqlite3.Row, recent: set[str]) -> tuple[sqlite3.Row | None, str]:
    """素材池：启用中的发帖素材，按语言/标签圈定，挑用得最少的；正文和最近发过的一样的往后排。"""
    lang = (_sp_get(sp, "pool_lang", "") or "").strip()
    tags = [t.strip() for t in (_sp_get(sp, "pool_tags", "") or "").replace("，", ",").split(",") if t.strip()]
    q = "SELECT * FROM materials WHERE kind='post' AND status='active' AND deleted_at IS NULL"
    args: list = []
    if lang:
        q += " AND lang=?"; args.append(lang)
    rows = conn.execute(q + " ORDER BY usage_count ASC, COALESCE(last_used_at,'') ASC, id ASC", args).fetchall()
    if tags:
        rows = [m for m in rows if set(x.strip() for x in (m["scenario_tags"] or "").split(",")) & set(tags)]
    if not rows:
        return None, ("素材池是空的：没有" + (f"语言为「{lang}」" if lang else "") + (f"、标签含 {'/'.join(tags)}" if tags else "")
                      + "的启用中发帖素材。到素材库添加（类型选「发帖」并启用）")
    fresh = [m for m in rows if m["text"] not in recent]
    if fresh:
        return fresh[0], f"素材池轮流：选用用得最少的一条（#{fresh[0]['id']}，已用 {fresh[0]['usage_count']} 次）"
    return rows[0], f"素材池里的 {len(rows)} 条最近 {RECENT_POST_DAYS} 天内都发过了，只能重用 #{rows[0]['id']}——建议开「AI 改写变体」或补充素材"


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

    def compose_post(self, conn: sqlite3.Connection, sp: sqlite3.Row) -> tuple[str | None, int | None, str, list[str]]:
        """按计划的内容来源产出正文。返回 (正文, material_id, 说明, 附件列表)；正文为 None 表示失败，说明里是原因。
        附件：固定素材 / 素材池用素材自带的；AI 按主题创作用计划上挂的。"""
        mode = _sp_get(sp, "content_mode", "fixed") or "fixed"
        recent_list = recent_post_texts(conn, sp["account_id"])
        recent = set(recent_list)
        note_parts: list[str] = []
        if mode == "ai_topic":
            brief = (_sp_get(sp, "ai_brief", "") or "").strip()
            plan_files = media.parse_files(_sp_get(sp, "media_files", "[]"))
            if not brief:
                return None, None, "计划选了「AI 按主题创作」但没填主题要求，请编辑计划补上", plan_files
            lang = (_sp_get(sp, "pool_lang", "") or "ja").strip() or "ja"
            try:
                res = self.llm.write_post(brief, lang, recent_list)
            except LLMError as e:
                return None, None, f"AI 按主题创作失败：{e}", plan_files
            return res["text"], None, "AI 按主题创作：" + (res.get("reason") or "") + (
                f"（带{media.describe(plan_files)}）" if plan_files else ""), plan_files
        if mode == "pool":
            mat, note = pick_pool_material(conn, sp, recent)
            if mat is None:
                return None, None, note, []
            note_parts.append(note)
        else:
            mat = conn.execute("SELECT * FROM materials WHERE id=? AND deleted_at IS NULL", (sp["material_id"],)).fetchone() \
                if sp["material_id"] else None
            if mat is None:
                return None, None, "计划绑定的素材已删除或进了回收站，请编辑计划重新选", []
            note_parts.append("固定素材")
        text = mat["text"]
        if _sp_get(sp, "ai_rewrite", 0):
            try:
                res = self.llm.rewrite_post(text, mat["lang"], recent_list)
                text = res["text"]
                note_parts.append("AI 改写变体：" + (res.get("reason") or ""))
            except LLMError as e:
                note_parts.append(f"AI 改写失败（{str(e)[:80]}），用素材原文")
        if text in recent and not _sp_get(sp, "ai_rewrite", 0):
            note_parts.append(f"⚠ 正文和最近 {RECENT_POST_DAYS} 天内发过的一样，X 可能判定重复而拒绝；建议开「AI 改写变体」")
        files = media.parse_files(mat["media_files"])
        if files:
            note_parts.append("带" + media.describe(files))
        return text, mat["id"], "；".join(note_parts), files

    def enqueue_from_plan(self, conn: sqlite3.Connection, sp: sqlite3.Row) -> tuple[bool, str]:
        """按定时计划生成一条「发帖」队列条目（不提交）。返回 (是否成功, 说明/错误)。"""
        text, mid, note, files = self.compose_post(conn, sp)
        if text is None:
            return False, note
        status = "approved" if sp["auto_approve"] else "pending"
        conn.execute(
            "INSERT INTO review_queue(account_id, action_type, material_id, scheduled_post_id, "
            "final_text, final_media_files, llm_reason, auto_approve, status, origin, created_at) "
            "VALUES (?,'post',?,?,?,?,?,?,?,'scheduled',?)",
            (sp["account_id"], mid, sp["id"], text, media.dump_files(files), note, sp["auto_approve"], status, utcnow_iso()))
        return True, note

    def fire_plan_now(self, sp_id: int) -> tuple[bool, str]:
        """「立即生成一次」：不等到点、不改下次运行时间。可能调 LLM，放线程池里跑。"""
        with get_conn() as conn:
            sp = conn.execute("SELECT * FROM scheduled_posts WHERE id=?", (sp_id,)).fetchone()
            if sp is None:
                return False, "计划不存在"
            ok, msg = self.enqueue_from_plan(conn, sp)
            conn.execute("UPDATE scheduled_posts SET last_run_at=?, last_error=? WHERE id=?",
                         (utcnow_iso(), None if ok else msg, sp_id))
            conn.commit()
        return ok, msg

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
                    ok, msg = self.enqueue_from_plan(conn, sp)
                    if not ok:
                        # 固定素材被删 → 暂停计划（不发空内容）；其他失败（LLM 出错、素材池空）记下原因，下次到点再试
                        log.warning("定时计划 #%s 未生成：%s", sp["id"], msg)
                        pause = (_sp_get(sp, "content_mode", "fixed") or "fixed") == "fixed"
                        conn.execute("UPDATE scheduled_posts SET last_error=?" + (", status='paused'" if pause else "") + " WHERE id=?",
                                     (msg, sp["id"]))
                        conn.commit()
                        continue
                    conn.execute("UPDATE scheduled_posts SET last_error=NULL WHERE id=?", (sp["id"],))
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
