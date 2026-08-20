"""设置（design-v1.1 §8.7）：运行模式、LLM、合规参数、预算、账号、黑名单。"""
from __future__ import annotations

import sqlite3

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
        client = LLMClient()
        if not client.configured:
            ui.notify("未配置网关：当前走启发式兜底（关键词规则打分/匹配，离线可测）。"
                      "填入 base_url + api_key 并保存后再测，即可切真实 LLM。", type="warning")
            return
        try:
            client.ping()
            ui.notify("连接成功 ✅ 打分/匹配将走真实 LLM", type="positive")
        except Exception as e:
            ui.notify(f"连接失败：{e}", type="negative")

    with ui.row():
        ui.button("保存", on_click=save).props("color=primary")
        ui.button("测试连接", on_click=test).props("outline")
    ui.label("提示：先「保存」再「测试连接」，测试用的是已保存的配置。").classes("text-xs text-gray-400")


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
    ui.label("发帖 / 回复账号管理").classes("font-semibold")
    ui.label("Mock 演示模式下也需要至少一个账号来承载发送；接真实凭据时把「凭据引用名」指向 "
             "secrets.toml 里的键。主号只能用官方通道（封号风险约束）。").classes("text-xs text-gray-400")
    body = ui.column().classes("w-full gap-2")

    def save_account(data: dict, existing_id: int | None = None) -> bool:
        handle = data["handle"].lstrip("@").strip()
        if not handle:
            ui.notify("请填写 handle", type="negative"); return False
        if data["is_primary"] and data["access_type"] == "unofficial":
            ui.notify("主号不能使用非官方（twifork）通道——封号风险过高", type="negative"); return False
        if data["max_interval_sec"] < data["min_interval_sec"]:
            ui.notify("最大间隔需 ≥ 最小间隔", type="negative"); return False
        try:
            with get_conn() as conn:
                if data["is_primary"]:  # 主号唯一：先清空其他主号
                    conn.execute("UPDATE accounts SET is_primary=0")
                if existing_id:
                    conn.execute(
                        "UPDATE accounts SET handle=?, display_name=?, access_type=?, is_primary=?, "
                        "credential_ref=?, daily_post_limit=?, daily_reply_limit=?, "
                        "min_interval_sec=?, max_interval_sec=? WHERE id=?",
                        (handle, data["display_name"], data["access_type"], 1 if data["is_primary"] else 0,
                         data["credential_ref"], data["daily_post_limit"], data["daily_reply_limit"],
                         data["min_interval_sec"], data["max_interval_sec"], existing_id))
                else:
                    conn.execute(
                        "INSERT INTO accounts(handle, display_name, access_type, is_primary, credential_ref, "
                        "daily_post_limit, daily_reply_limit, min_interval_sec, max_interval_sec, created_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (handle, data["display_name"], data["access_type"], 1 if data["is_primary"] else 0,
                         data["credential_ref"], data["daily_post_limit"], data["daily_reply_limit"],
                         data["min_interval_sec"], data["max_interval_sec"], utcnow_iso()))
                conn.commit()
        except sqlite3.IntegrityError as e:
            ui.notify(f"保存失败：handle 可能重复或违反约束（{e}）", type="negative"); return False
        factory.invalidate()
        ui.notify("已保存", type="positive")
        return True

    def open_dialog(existing: sqlite3.Row | None = None):
        with ui.dialog() as dlg, ui.card().classes("w-96"):
            ui.label("编辑账号" if existing else "添加账号").classes("text-lg font-bold")
            handle = ui.input("handle（不含 @）", value=existing["handle"] if existing else "") \
                .classes("w-full").props("outlined dense")
            dname = ui.input("显示名", value=existing["display_name"] if existing else "") \
                .classes("w-full").props("outlined dense")
            atype = ui.select({"official": "官方 API（可作主号）", "unofficial": "非官方 twifork（仅小号）"},
                              value=existing["access_type"] if existing else "official", label="通道类型") \
                .classes("w-full").props("outlined dense")
            primary = ui.switch("设为主号", value=bool(existing["is_primary"]) if existing else False)
            cref = ui.input("凭据引用名（secrets.toml 的键；Mock 模式可留空）",
                            value=existing["credential_ref"] if existing else "") \
                .classes("w-full").props("outlined dense")
            with ui.row().classes("w-full gap-2 no-wrap"):
                post_lim = ui.number("日发帖上限", value=existing["daily_post_limit"] if existing else 10, min=0) \
                    .props("outlined dense").classes("flex-1")
                reply_lim = ui.number("日回复上限", value=existing["daily_reply_limit"] if existing else 15, min=0) \
                    .props("outlined dense").classes("flex-1")
            with ui.row().classes("w-full gap-2 no-wrap"):
                mn = ui.number("最小间隔(秒)", value=existing["min_interval_sec"] if existing else 180, min=0) \
                    .props("outlined dense").classes("flex-1")
                mx = ui.number("最大间隔(秒)", value=existing["max_interval_sec"] if existing else 600, min=0) \
                    .props("outlined dense").classes("flex-1")

            def do_save():
                ok = save_account({
                    "handle": handle.value or "",
                    "display_name": dname.value or "",
                    "access_type": atype.value,
                    "is_primary": bool(primary.value),
                    "credential_ref": cref.value or "",
                    "daily_post_limit": int(post_lim.value or 0),
                    "daily_reply_limit": int(reply_lim.value or 0),
                    "min_interval_sec": int(mn.value or 0),
                    "max_interval_sec": int(mx.value or 0),
                }, existing["id"] if existing else None)
                if ok:
                    dlg.close(); render()

            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("取消", on_click=dlg.close).props("flat")
                ui.button("保存", on_click=do_save).props("color=primary")
        dlg.open()

    def set_primary(aid: int):
        with get_conn() as conn:
            row = conn.execute("SELECT access_type FROM accounts WHERE id=?", (aid,)).fetchone()
            if row is None:
                return
            if row["access_type"] == "unofficial":
                ui.notify("非官方账号不能设为主号", type="negative"); return
            conn.execute("UPDATE accounts SET is_primary=0")
            conn.execute("UPDATE accounts SET is_primary=1 WHERE id=?", (aid,))
            conn.commit()
        factory.invalidate()
        ui.notify("已设为主号", type="positive"); render()

    def del_account(aid: int):
        try:
            with get_conn() as conn:
                conn.execute("DELETE FROM accounts WHERE id=?", (aid,))
                conn.commit()
            factory.invalidate(aid)
            ui.notify("已删除", type="positive")
        except sqlite3.IntegrityError:
            ui.notify("该账号已有关联记录（发送/队列/定时），无法删除；请改为「暂停」", type="negative")
        render()

    def render():
        body.clear()
        with get_conn() as conn:
            rows = conn.execute("SELECT * FROM accounts ORDER BY is_primary DESC, id").fetchall()
        with body:
            with ui.row().classes("items-center justify-between w-full"):
                ui.label(f"共 {len(rows)} 个账号").classes("text-sm text-gray-400")
                ui.button("添加账号", icon="add", on_click=lambda: open_dialog()).props("color=primary")
            if not rows:
                ui.label("暂无账号，点右上「添加账号」新建一个。").classes("text-gray-400")
            for a in rows:
                with ui.card().classes("w-full"):
                    with ui.row().classes("items-center gap-2"):
                        ui.label(f"@{a['handle']}").classes("font-semibold")
                        if a["display_name"]:
                            ui.label(a["display_name"]).classes("text-xs text-gray-400")
                        ui.badge("官方" if a["access_type"] == "official" else "非官方").classes("bg-slate-500")
                        if a["is_primary"]:
                            ui.badge("主号 ★").classes("bg-amber-500")
                        ui.badge(a["status"]).classes("bg-green-600" if a["status"] == "active" else "bg-red-600")
                    ui.label(f"日发帖 {a['daily_post_limit']} / 日回复 {a['daily_reply_limit']} · "
                             f"间隔 {a['min_interval_sec']}-{a['max_interval_sec']}s · "
                             f"活跃 {a['active_hours_start']}-{a['active_hours_end']} {a['timezone']}").classes("text-xs text-gray-400")
                    with ui.row().classes("gap-1 flex-wrap"):
                        ui.button("测试连接", on_click=lambda aa=a: _test_conn(aa)).props("flat dense")
                        ui.button("编辑", on_click=lambda aa=a: open_dialog(aa)).props("flat dense")
                        if not a["is_primary"] and a["access_type"] == "official":
                            ui.button("设为主号", on_click=lambda aid=a["id"]: set_primary(aid)).props("flat dense")
                        if a["status"] == "active":
                            ui.button("暂停", on_click=lambda aid=a["id"]: (_set_acc_status(aid, "paused"), render())).props("flat dense")
                        else:
                            ui.button("启用", on_click=lambda aid=a["id"]: (_set_acc_status(aid, "active"), render())).props("flat dense")
                        ui.button("删除", on_click=lambda aid=a["id"]: del_account(aid)).props("flat dense color=negative")
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
