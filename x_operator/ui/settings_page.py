"""设置（design-v1.1 §8.7）：运行模式、LLM、合规参数、预算、账号、黑名单。"""
from __future__ import annotations

from nicegui import ui

from .. import config
from ..adapters import factory
from ..llm.client import LLMClient
from ..db.database import get_conn, utcnow_iso
from .layout import shell


def register(jobs) -> None:
    @ui.page("/settings")
    def settings_page():
        with shell("/settings"):
            ui.label("设置").classes("text-2xl font-bold")

            with ui.tabs().classes("w-full") as tabs:
                t_run = ui.tab("运行模式")
                t_llm = ui.tab("LLM")
                t_comp = ui.tab("合规参数")
                t_budget = ui.tab("预算")
                t_acc = ui.tab("账号")
                t_bl = ui.tab("黑名单")

            with ui.tab_panels(tabs, value=t_run).classes("w-full"):
                with ui.tab_panel(t_run):
                    _run_panel()
                with ui.tab_panel(t_llm):
                    _llm_panel()
                with ui.tab_panel(t_comp):
                    _numeric_panel([
                        ("cooldown_days", "作者冷却天数"),
                        ("grace_period_hours", "定时补发宽限小时"),
                        ("reply_ttl_hours", "回复条目时效小时"),
                        ("tweet_max_age_hours", "推文最大年龄小时"),
                        ("nurture_days", "养号期天数"),
                        ("match_confidence_threshold", "匹配置信度阈值(0-1)"),
                    ])
                with ui.tab_panel(t_budget):
                    _numeric_panel([
                        ("daily_read_budget", "每日读额度"),
                        ("budget_reserve_reads", "熔断保留读额度"),
                        ("monthly_budget_usd", "月预算(USD)"),
                        ("monitor_interval_minutes", "监控间隔(分钟)"),
                        ("search_runs_per_day", "每日搜索次数"),
                    ])
                with ui.tab_panel(t_acc):
                    _accounts_panel()
                with ui.tab_panel(t_bl):
                    _blacklist_panel()


def _run_panel():
    ui.label("运行模式").classes("font-semibold")
    dry = ui.switch("Mock 演示模式（不碰真实 X，零凭据零风险）", value=config.get_bool("dry_run", True))

    def on_dry(e):
        config.set_value("dry_run", bool(e.args))
        factory.invalidate()
        ui.notify("已切换运行模式（重开页面顶栏徽标生效）", type="positive")
    dry.on("update:model-value", on_dry)

    auto = ui.switch("启用后台自动轮询（监控/搜索按间隔自动跑）", value=config.get_bool("auto_jobs_enabled", False))
    auto.on("update:model-value", lambda e: (config.set_value("auto_jobs_enabled", bool(e.args)),
                                             ui.notify("已更新自动轮询开关", type="positive")))
    ui.label("测试期建议保持关闭，用各页面的「运行一次」按钮手动触发，便于观察每一步。").classes("text-xs text-gray-400")


def _llm_panel():
    ui.label("LLM 网关（OpenAI 兼容；留空则用启发式兜底，离线可测）").classes("font-semibold")
    base = ui.input("base_url", value=config.get("llm_base_url") or "").classes("w-full").props("outlined")
    key = ui.input("api_key", value=config.get("llm_api_key") or "", password=True).classes("w-full").props("outlined")
    light = ui.input("轻量模型", value=config.get("llm_model_light") or "").classes("w-full").props("outlined")
    strong = ui.input("强模型", value=config.get("llm_model_strong") or "").classes("w-full").props("outlined")

    def save():
        config.set_value("llm_base_url", base.value.strip())
        config.set_value("llm_api_key", key.value.strip())
        config.set_value("llm_model_light", light.value.strip())
        config.set_value("llm_model_strong", strong.value.strip())
        ui.notify("已保存", type="positive")

    def test():
        try:
            LLMClient().ping()
            ui.notify("连接成功", type="positive")
        except Exception as e:
            ui.notify(f"连接失败：{e}", type="negative")

    with ui.row():
        ui.button("保存", on_click=save).props("color=primary")
        ui.button("测试连接", on_click=test).props("outline")


def _numeric_panel(fields):
    inputs = {}
    for key, label in fields:
        inputs[key] = ui.input(label, value=config.get(key) or "").classes("w-full").props("outlined")

    def save():
        for key, inp in inputs.items():
            config.set_value(key, inp.value.strip())
        ui.notify("已保存", type="positive")
    ui.button("保存", on_click=save).props("color=primary")


def _accounts_panel():
    body = ui.column().classes("w-full gap-2")

    def render():
        body.clear()
        with get_conn() as conn:
            rows = conn.execute("SELECT * FROM accounts ORDER BY id").fetchall()
        with body:
            for a in rows:
                with ui.card().classes("w-full"):
                    with ui.row().classes("items-center gap-2"):
                        ui.label(f"@{a['handle']}").classes("font-semibold")
                        ui.badge(a["access_type"]).classes("bg-slate-500")
                        if a["is_primary"]:
                            ui.badge("主号 ★").classes("bg-amber-500")
                        ui.badge(a["status"]).classes("bg-green-600" if a["status"] == "active" else "bg-red-600")
                    ui.label(f"日发帖 {a['daily_post_limit']} / 日回复 {a['daily_reply_limit']} · "
                             f"间隔 {a['min_interval_sec']}-{a['max_interval_sec']}s · "
                             f"活跃 {a['active_hours_start']}-{a['active_hours_end']} {a['timezone']}").classes("text-xs text-gray-400")
                    with ui.row().classes("gap-2"):
                        ui.button("测试连接", on_click=lambda aa=a: _test_conn(aa)).props("flat")
                        if a["status"] == "active":
                            ui.button("暂停", on_click=lambda aa=a: (_set_acc_status(aa["id"], "paused"), render())).props("flat")
                        else:
                            ui.button("启用", on_click=lambda aa=a: (_set_acc_status(aa["id"], "active"), render())).props("flat")
    render()


def _test_conn(a):
    try:
        client = factory.get_client(a)
        user = client.get_me()
        ui.notify(f"连接成功：@{user.handle}", type="positive")
    except Exception as e:
        ui.notify(f"连接失败：{e}", type="negative")


def _set_acc_status(aid, status):
    with get_conn() as conn:
        conn.execute("UPDATE accounts SET status=? WHERE id=?", (status, aid))
        conn.commit()
    factory.invalidate(aid)


def _blacklist_panel():
    inp = ui.input("@handle 或 user_id").props("outlined dense")
    body = ui.column().classes("w-full gap-1")

    def add():
        v = inp.value.strip().lstrip("@")
        if not v:
            return
        with get_conn() as conn:
            conn.execute("INSERT INTO blacklist(x_user_id, handle, reason, created_at) VALUES (?,?,?,?) "
                         "ON CONFLICT(x_user_id) DO NOTHING", (v, v, "手动添加", utcnow_iso()))
            conn.commit()
        inp.value = ""; render(); ui.notify("已添加", type="positive")

    def render():
        body.clear()
        with get_conn() as conn:
            rows = conn.execute("SELECT * FROM blacklist ORDER BY id DESC").fetchall()
        with body:
            for b in rows:
                with ui.row().classes("items-center gap-2"):
                    ui.label(f"{b['handle'] or b['x_user_id']}").classes("text-sm")
                    ui.label(b["reason"]).classes("text-xs text-gray-400")
                    ui.button("移除", on_click=lambda bb=b: (_del_bl(bb["id"]), render())).props("flat dense color=negative")

    ui.button("添加", on_click=add).props("color=primary")
    render()


def _del_bl(bid):
    with get_conn() as conn:
        conn.execute("DELETE FROM blacklist WHERE id=?", (bid,))
        conn.commit()
