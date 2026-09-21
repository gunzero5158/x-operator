"""仪表盘（design-v1.1 §8.1）：概况卡 + 读额度 + 最近异常 + 手动运行入口。"""
from __future__ import annotations

from .i18n import describe_schedule, Labels, t as _tr

from nicegui import ui

from .. import config
from ..core import budget
from ..core.monitor import get_read_account, read_is_billed
from ..core.readpool import pool_status
from ..core.scheduler import AUTO_JOBS, dispatch_interval_seconds, job_enabled, next_runs
from ..db.database import get_conn, to_iso
from .daily_usage import DailyUsagePanel, RecentErrorsPanel
from .layout import page_title, detail_text
from .layout import fmt_time, run_job, run_job_with_progress, shell, display_tz


def _stats() -> dict:
    with get_conn() as conn:
        pending = conn.execute("SELECT COUNT(*) AS c FROM review_queue WHERE status='pending'").fetchone()["c"]
        approved = conn.execute("SELECT COUNT(*) AS c FROM review_queue WHERE status='approved'").fetchone()["c"]
        no_match = conn.execute("SELECT COUNT(*) AS c FROM target_tweets WHERE process_status='no_match'").fetchone()["c"]
        total_targets = conn.execute("SELECT COUNT(*) AS c FROM target_tweets").fetchone()["c"]
        targets_today = conn.execute(
            "SELECT COUNT(*) AS c FROM target_tweets WHERE fetched_at>=strftime('%Y-%m-%dT00:00:00Z','now')").fetchone()["c"]
        fails = conn.execute(
            "SELECT COUNT(*) AS c FROM action_log WHERE success=0 AND created_at>=strftime('%Y-%m-%dT%H:%M:%SZ','now','-1 day')"
        ).fetchone()["c"]
        recent_fail = conn.execute(
            "SELECT id, created_at, endpoint, error FROM action_log WHERE success=0 ORDER BY created_at DESC, id DESC LIMIT 20"
        ).fetchall()
        accounts = conn.execute("SELECT handle, status, access_type, is_primary, credentials FROM accounts WHERE deleted_at IS NULL ORDER BY is_primary DESC, id").fetchall()
        watched = conn.execute("SELECT COUNT(*) AS c FROM watched_users WHERE enabled=1").fetchone()["c"]
        rules = conn.execute("SELECT COUNT(*) AS c FROM search_rules WHERE enabled=1").fetchone()["c"]
        materials = conn.execute("SELECT COUNT(*) AS c FROM materials WHERE status='active' AND deleted_at IS NULL").fetchone()["c"]
    return dict(pending=pending, approved=approved, no_match=no_match,
                total_targets=total_targets, targets_today=targets_today, fails=fails,
                recent_fail=recent_fail, accounts=accounts, watched=watched, rules=rules, materials=materials)


def register(jobs) -> None:
    @ui.page("/")
    def dashboard_page():
        with shell("/"):
            page_title(_tr('仪表盘'), _tr('运行概况与需要关注的事项'))

            overview = ui.row().classes("xo-kpi-grid w-full")
            usage = DailyUsagePanel()
            body = ui.column().classes("xo-dashboard w-full")
            errors = RecentErrorsPanel()

            def render():
                body.clear()
                s = _stats()
                overview.clear()
                with overview:
                    _card(_tr('今日发送'), str(sum(r["post_used"] + r["reply_used"] for r in usage.rows)), _tr('按各账号当地日期汇总'))
                    _card(_tr('待审核'), str(s["pending"]), _tr('任务队列积压'), warn=s["pending"] > 0, link="/queue")
                    _card(_tr('待发送'), str(s["approved"]), _tr('已批准、等分发器发出'), link="/queue")
                    _card(_tr('今日抓取'), str(s["targets_today"]), _tr('累计 {p0} 条', p0=s['total_targets']), link="/targets")
                    rate = f'{(s["no_match"] / s["total_targets"] * 100):.0f}%' if s["total_targets"] else "—"
                    _card(_tr('未匹配率'), rate, _tr('抓取后没配到素材的比例'))
                    _card(_tr('24h 异常'), str(s["fails"]), _tr('失败的 API/LLM 调用'), warn=s["fails"] > 0)

                with body:
                    # 就绪检查：告诉用户为什么可能「跑了没结果」
                    with ui.card().classes("xo-overview-panel w-full"):
                        ui.label(_tr('就绪检查')).classes("font-semibold")
                        from ..adapters.real import credentials_ready, parse_credentials
                        active_accounts = [a for a in s["accounts"] if a["status"] == "active"]
                        _check(bool(active_accounts), _tr('启用的账号：{p0} 个', p0=len(active_accounts)), _tr('没有启用的账号 → 抓取和发送都跑不了。去「设置 → 账号」添加'), "/settings")
                        if active_accounts:
                            missing = [a["handle"] for a in active_accounts
                                       if not credentials_ready(a["access_type"], parse_credentials(a["credentials"]))[0]]
                            _check(not missing, _tr('所有启用账号都已填凭据'),
                                   _tr('这些账号还没填凭据：@{p0} → 「设置 → 账号 → 编辑 / 填凭据」', p0='、@'.join(missing[:5]) + (f' … ({len(missing)})' if len(missing) > 5 else '')), "/settings")
                        _check(s["watched"] > 0 or s["rules"] > 0, _tr('监控推主 {p0} 个 · 搜索规则 {p1} 条', p0=s['watched'], p1=s['rules']),
                               _tr('没有监控推主也没有搜索规则 → 没有抓取来源'), "/watched")
                        _check(s["materials"] > 0, _tr('启用的素材 {p0} 条', p0=s['materials']), _tr('没有启用的素材 → 抓到推文也匹配不到回复'), "/materials")
                        llm_ok = jobs.llm.configured
                        _check(True, "LLM：" + (_tr('已配置网关（真实 LLM 打分/匹配）') if llm_ok else _tr('未配置，用关键词启发式兜底（可用但粗糙）')), "", "/settings")

                    with ui.card().classes("xo-overview-panel w-full"):
                        b = budget.current()
                        ra = get_read_account()
                        billed = ra is not None and read_is_billed(ra)
                        ui.label(_tr('抓取账号池与读额度')).classes("font-semibold")
                        ps = pool_status()
                        if not ps:
                            ui.label(_tr('没有启用的账号。设置 → 账号 添加。')).classes("text-sm")
                        else:
                            lines = []
                            for x in ps:
                                who = (_tr('官方号') if x["official"] else _tr('小号')) + f" @{x['handle']}"
                                if x["official"] and not x["participates"]:
                                    lines.append(who + _tr('：不参与抓取（设置 → 抓取 可打开）'))
                                elif x["paused_until"]:
                                    lines.append(who + _tr('：撞过 429，暂停到 {p0}', p0=fmt_time(to_iso(x['paused_until']))))
                                else:
                                    lines.append(who + _tr('：最近 15 分钟 {p0}/{p1} 次', p0=x['requests'], p1=x['cap']))
                            detail_text(_tr('抓取账号池详情'), "；".join(lines) + _tr('。下一次请求会用 ') + (f"@{ra['handle']}" if ra else _tr('（现在没有能用的号）'))
                                     + _tr('。设置 → 抓取 可调上限。'))
                        r_uid, r_at = jobs.monitor.pending_resume()
                        if r_uid is not None and r_at is not None:
                            ui.label(_tr('⏸ 监控上次因限流暂停，{p0} 自动从停下的推主继续', p0=fmt_time(to_iso(r_at)))).classes("text-xs text-orange-600")
                        pct = min(1.0, b.used_today / b.daily_budget) if b.daily_budget else 0
                        ui.linear_progress(pct, show_value=False).classes("w-full")
                        month_usd = b.used_month * budget.OFFICIAL_READ_USD
                        monthly_budget = config.get_float("monthly_budget_usd", 60)
                        ui.label(_tr('今日官方 API 读取 {p0}/{p1} 条（熔断保留 {p2}）· 本月累计 {p3} 条，按 ${p4}/条估算约 ${p5} / 月预算 ${p6}（实际单价以开发者后台为准）· 今日小号通道读取 {p7} 条（免费）', p0=b.used_today, p1=b.daily_budget, p2=b.reserve, p3=b.used_month, p4=budget.OFFICIAL_READ_USD, p5=format(month_usd, '.2f'), p6=format(monthly_budget, '.0f'), p7=b.free_today)).classes("text-xs text-gray-500")
                        if not billed:
                            ui.label(_tr('下一次请求走小号：读额度限制不生效，只在轮到官方号时才会拦。')).classes("text-xs text-gray-400")
                        else:
                            denied = b.allow(auto=True)
                            if denied:
                                ui.label(denied).classes("text-xs text-orange-600")

                    with ui.card().classes("w-full"):
                        ui.label(_tr('快捷操作')).classes("font-semibold")
                        nr = next_runs(jobs.scheduler)
                        parts = []
                        for jid, (jname, _d, _k, _dm, _dt) in AUTO_JOBS.items():
                            parts.append(f"{jname}：" + ((_tr('开，下次 ') + nr[jid].astimezone(display_tz()).strftime("%m-%d %H:%M") + "，" + describe_schedule(jid)) if nr.get(jid) else _tr('关')))
                        parts.append(_tr('发送分发：') + (_tr('开（每 {p0} 秒）', p0=dispatch_interval_seconds()) if job_enabled("dispatcher") else _tr('关')))
                        detail_text(_tr('自动运行计划'), "；".join(parts))
                        with ui.row().classes("gap-2 flex-wrap"):
                            ui.button(_tr('运行监控轮询'), icon="visibility",
                                      on_click=lambda: run_job_with_progress(lambda progress: jobs.monitor.run_once(progress=progress), _tr('监控'), render, task_key='monitor',
                                                                             result_link=(_tr('查看抓取记录'), "/targets?source=monitor"))).props("outline")
                            ui.button(_tr('运行所有搜索规则'), icon="manage_search",
                                      on_click=lambda: run_job_with_progress(lambda progress: jobs.search.run_once(progress=progress), _tr('搜索'), render, task_key='search',
                                                                             result_link=(_tr('查看抓取记录'), "/targets?source=search"))).props("outline")
                            ui.button(_tr('生成到点定时推文'), icon="schedule", on_click=lambda: _run_sched(jobs, render)).props("outline")
                            ui.button(_tr('触发发送分发'), icon="send", on_click=lambda: run_job(jobs.dispatcher.tick, _tr('发送'), render)).props("outline")
                        ui.label(_tr('运行结果会弹出提示；抓到的推文去「抓取记录」看，生成的回复去「任务队列」看。')).classes("text-xs text-gray-400")

                errors.refresh(s)

            render()
            ui.timer(5.0, usage.refresh)
            ui.timer(30.0, render)


def _card(title: str, value: str, sub: str, warn: bool = False, link: str | None = None):
    with ui.card().classes("xo-kpi" + (" is-warning" if warn else "")):
        ui.label(title).classes("text-sm text-gray-500")
        ui.label(value).classes("text-3xl font-bold " + ("text-red-500" if warn else ""))
        if link:
            ui.link(sub + " →", link).classes("text-xs text-gray-400")
        else:
            ui.label(sub).classes("text-xs text-gray-400")


def _check(ok: bool, ok_text: str, bad_text: str, link: str):
    with ui.row().classes("items-center gap-2"):
        ui.icon("check_circle" if ok else "error").classes("text-green-600" if ok else "text-red-500")
        if ok:
            ui.label(ok_text).classes("text-sm")
        else:
            ui.link(bad_text, link).classes("text-sm text-red-500")


def _run_sched(jobs, refresh) -> None:
    try:
        n = jobs.run_scheduled_posts()
        ui.notify(_tr('生成 {p0} 条定时推文到任务队列', p0=n) if n else _tr('没有到点的定时发帖计划（到「定时发帖计划」页可「立即生成一次」）'), type="positive" if n else "info")
    except Exception as e:
        ui.notify(_tr('出错：{p0}', p0=e), type="negative")
    refresh()


# Resolve display labels per client; keep core dictionaries and stored values unchanged.
AUTO_JOBS = Labels(AUTO_JOBS)
