"""仪表盘（design-v1.1 §8.1）：概况卡 + 读额度 + 最近异常 + 手动运行入口。"""
from __future__ import annotations

from nicegui import ui

from .. import config
from ..db.database import get_conn
from .layout import fmt_time, run_job, shell


def _stats() -> dict:
    with get_conn() as conn:
        sent_today = conn.execute(
            "SELECT COUNT(*) AS c FROM interactions WHERE sent_at>=strftime('%Y-%m-%dT00:00:00Z','now')"
        ).fetchone()["c"]
        pending = conn.execute("SELECT COUNT(*) AS c FROM review_queue WHERE status='pending'").fetchone()["c"]
        approved = conn.execute("SELECT COUNT(*) AS c FROM review_queue WHERE status='approved'").fetchone()["c"]
        no_match = conn.execute("SELECT COUNT(*) AS c FROM target_tweets WHERE process_status='no_match'").fetchone()["c"]
        total_targets = conn.execute("SELECT COUNT(*) AS c FROM target_tweets").fetchone()["c"]
        targets_today = conn.execute(
            "SELECT COUNT(*) AS c FROM target_tweets WHERE fetched_at>=strftime('%Y-%m-%dT00:00:00Z','now')").fetchone()["c"]
        fails = conn.execute(
            "SELECT COUNT(*) AS c FROM action_log WHERE success=0 AND created_at>=strftime('%Y-%m-%dT%H:%M:%SZ','now','-1 day')"
        ).fetchone()["c"]
        reads_today = conn.execute(
            "SELECT COALESCE(SUM(reads_consumed),0) AS c FROM action_log WHERE created_at>=strftime('%Y-%m-%dT00:00:00Z','now')"
        ).fetchone()["c"]
        recent_fail = conn.execute(
            "SELECT created_at, endpoint, error FROM action_log WHERE success=0 ORDER BY created_at DESC LIMIT 10"
        ).fetchall()
        accounts = conn.execute("SELECT handle, status, access_type, is_primary, credentials FROM accounts ORDER BY is_primary DESC, id").fetchall()
        watched = conn.execute("SELECT COUNT(*) AS c FROM watched_users WHERE enabled=1").fetchone()["c"]
        rules = conn.execute("SELECT COUNT(*) AS c FROM search_rules WHERE enabled=1").fetchone()["c"]
        materials = conn.execute("SELECT COUNT(*) AS c FROM materials WHERE status='active' AND deleted_at IS NULL").fetchone()["c"]
    return dict(sent_today=sent_today, pending=pending, approved=approved, no_match=no_match,
                total_targets=total_targets, targets_today=targets_today, fails=fails, reads_today=reads_today,
                recent_fail=recent_fail, accounts=accounts, watched=watched, rules=rules, materials=materials)


def register(jobs) -> None:
    @ui.page("/")
    def dashboard_page():
        with shell("/"):
            ui.label("仪表盘").classes("text-2xl font-bold")

            body = ui.column().classes("w-full gap-4")

            def render():
                body.clear()
                s = _stats()
                with body:
                    with ui.row().classes("gap-4 w-full"):
                        _card("今日发送", str(s["sent_today"]), "已发出的推文/回复")
                        _card("待审核", str(s["pending"]), "审核队列积压", warn=s["pending"] > 0, link="/queue")
                        _card("待发送", str(s["approved"]), "已批准、等分发器发出", link="/queue")
                        _card("今日抓取", str(s["targets_today"]), f"累计 {s['total_targets']} 条", link="/targets")
                        rate = f'{(s["no_match"] / s["total_targets"] * 100):.0f}%' if s["total_targets"] else "—"
                        _card("未匹配率", rate, "抓取后没配到素材的比例")
                        _card("24h 异常", str(s["fails"]), "失败的 API/LLM 调用", warn=s["fails"] > 0)

                    # 就绪检查：告诉用户为什么可能「跑了没结果」
                    with ui.card().classes("w-full"):
                        ui.label("就绪检查").classes("font-semibold")
                        from ..adapters.real import credentials_ready, parse_credentials
                        active_accounts = [a for a in s["accounts"] if a["status"] == "active"]
                        _check(bool(active_accounts), f"启用的账号：{len(active_accounts)} 个", "没有启用的账号 → 抓取和发送都跑不了。去「设置 → 账号」添加", "/settings")
                        if active_accounts:
                            missing = [a["handle"] for a in active_accounts
                                       if not credentials_ready(a["access_type"], parse_credentials(a["credentials"]))[0]]
                            _check(not missing, "所有启用账号都已填凭据",
                                   f"这些账号还没填凭据：@{'、@'.join(missing)} → 「设置 → 账号 → 编辑 / 填凭据」", "/settings")
                        _check(s["watched"] > 0 or s["rules"] > 0, f"监控推主 {s['watched']} 个 · 搜索规则 {s['rules']} 条",
                               "没有监控推主也没有搜索规则 → 没有抓取来源", "/watched")
                        _check(s["materials"] > 0, f"启用的素材 {s['materials']} 条", "没有启用的素材 → 抓到推文也匹配不到回复", "/materials")
                        llm_ok = bool(config.get("llm_base_url") and config.get("llm_api_key"))
                        _check(True, "LLM：" + ("已配置网关（真实 LLM 打分/匹配）" if llm_ok else "未配置，用关键词启发式兜底（可用但粗糙）"), "", "/settings")

                    with ui.card().classes("w-full"):
                        daily_budget = config.get_int("daily_read_budget", 330)
                        ui.label("读额度").classes("font-semibold")
                        pct = min(1.0, s["reads_today"] / daily_budget) if daily_budget else 0
                        ui.linear_progress(pct, show_value=False).classes("w-full")
                        ui.label(f'今日 {s["reads_today"]}/{daily_budget} 次读取 · 计费模式 {config.get("billing_mode")}')

                    with ui.card().classes("w-full"):
                        ui.label("手动运行（测试期用；自动轮询可在设置页打开）").classes("font-semibold")
                        with ui.row().classes("gap-2 flex-wrap"):
                            ui.button("运行监控轮询", icon="visibility", on_click=lambda: run_job(jobs.monitor.run_once, "监控", render))
                            ui.button("运行语义搜索", icon="manage_search", on_click=lambda: run_job(jobs.search.run_once, "搜索", render))
                            ui.button("生成到点定时推文", icon="schedule", on_click=lambda: _run_sched(jobs, render))
                            ui.button("触发发送分发", icon="send", on_click=lambda: run_job(jobs.dispatcher.tick, "发送", render))
                        ui.label("运行结果会弹出提示；抓到的推文去「抓取记录」看，生成的回复去「审核队列」看。").classes("text-xs text-gray-400")

                    with ui.card().classes("w-full"):
                        ui.label("最近异常").classes("font-semibold")
                        if not s["recent_fail"]:
                            ui.label("暂无异常 ✨")
                        else:
                            rows = [{"时间": fmt_time(r["created_at"]), "来源": r["endpoint"], "说明": r["error"] or ""}
                                    for r in s["recent_fail"]]
                            ui.table(columns=[{"name": k, "label": k, "field": k, "align": "left"} for k in ("时间", "来源", "说明")],
                                     rows=rows).classes("w-full").props("wrap-cells")

            render()
            ui.timer(30.0, render)


def _card(title: str, value: str, sub: str, warn: bool = False, link: str | None = None):
    with ui.card().classes("min-w-36"):
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
        ui.notify(f"生成 {n} 条定时推文到审核队列" if n else "没有到点的定时计划（到「定时计划」页可「立即生成一次」）", type="positive" if n else "info")
    except Exception as e:
        ui.notify(f"出错：{e}", type="negative")
    refresh()
