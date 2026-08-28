"""设置（design-v1.1 §8.7）：运行模式、LLM、合规参数、预算、账号（含凭据）、黑名单、数据清理。"""
from __future__ import annotations

import json
import sqlite3

from nicegui import run, ui

from .. import config
from ..adapters import factory
from ..adapters.real import OFFICIAL_REQUIRED, parse_credentials
from ..db.database import get_conn, utcnow_iso
from ..llm.client import LLMClient
from .layout import confirm, shell

_TZ_OPTIONS = ["Asia/Tokyo", "Asia/Shanghai", "Asia/Taipei", "Asia/Singapore", "UTC",
               "America/New_York", "America/Los_Angeles", "Europe/London", "Europe/Berlin"]


def register(jobs) -> None:
    @ui.page("/settings")
    def settings_page():
        with shell("/settings"):
            ui.label("设置").classes("text-2xl font-bold")

            with ui.tabs().classes("w-full") as tabs:
                t_acc = ui.tab("账号")
                t_run = ui.tab("运行模式")
                t_llm = ui.tab("LLM")
                t_comp = ui.tab("合规参数")
                t_budget = ui.tab("预算")
                t_bl = ui.tab("黑名单")
                t_data = ui.tab("数据")

            with ui.tab_panels(tabs, value=t_acc).classes("w-full"):
                with ui.tab_panel(t_acc):
                    _accounts_panel()
                with ui.tab_panel(t_run):
                    _run_panel()
                with ui.tab_panel(t_llm):
                    _llm_panel()
                with ui.tab_panel(t_comp):
                    _numeric_panel([
                        ("cooldown_days", "作者冷却天数（对同一作者两次互动的最小间隔）"),
                        ("grace_period_hours", "定时补发宽限小时"),
                        ("reply_ttl_hours", "回复条目时效小时（待审核超过此时长自动过期）"),
                        ("tweet_max_age_hours", "推文最大年龄小时（更旧的推文不回复）"),
                        ("nurture_days", "养号期天数（新小号限额减半）"),
                        ("match_confidence_threshold", "匹配置信度阈值(0-1)"),
                    ])
                with ui.tab_panel(t_budget):
                    _numeric_panel([
                        ("daily_read_budget", "每日读额度（条）"),
                        ("budget_reserve_reads", "熔断保留读额度"),
                        ("monthly_budget_usd", "月预算(USD)"),
                        ("monitor_interval_minutes", "监控间隔(分钟)（改后需重启生效）"),
                        ("search_runs_per_day", "每日搜索次数"),
                    ])
                with ui.tab_panel(t_bl):
                    _blacklist_panel()
                with ui.tab_panel(t_data):
                    _data_panel()


def _run_panel():
    ui.label("运行模式").classes("font-semibold")
    dry = ui.switch("Mock 演示模式（不碰真实 X，零凭据零风险）", value=config.get_bool("dry_run", True))

    def on_dry(e):
        config.set_value("dry_run", bool(e.args))
        factory.invalidate()
        ui.notify("已切换运行模式" + ("：所有抓取/发送走假数据" if e.args else "：将使用账号里填的真实凭据抓取和发送！"),
                  type="positive" if e.args else "warning", multi_line=True, close_button=True)
    dry.on("update:model-value", on_dry)
    ui.label("关闭演示模式前，请先到「账号」标签填好凭据并点「测试连接」确认能连上。").classes("text-xs text-gray-400")

    auto = ui.switch("启用后台自动轮询（监控/搜索按间隔自动跑）", value=config.get_bool("auto_jobs_enabled", False))
    auto.on("update:model-value", lambda e: (config.set_value("auto_jobs_enabled", bool(e.args)),
                                             ui.notify("已更新自动轮询开关", type="positive")))
    ui.label("测试期建议保持关闭，用各页面的「运行一次」按钮手动触发，便于观察每一步。"
             "无论开关如何，「定时计划到点检查」和「发送分发」每分钟都会自动跑。").classes("text-xs text-gray-400")


def _llm_panel():
    ui.label("LLM 网关（OpenAI 兼容；留空则用启发式兜底，离线可测）").classes("font-semibold")
    base = ui.input("base_url（例：https://api.apimax.jp/v1）", value=config.get("llm_base_url") or "").classes("w-full").props("outlined")
    key = ui.input("api_key", value=config.get("llm_api_key") or "", password=True, password_toggle_button=True).classes("w-full").props("outlined")
    light = ui.input("轻量模型（打分用）", value=config.get("llm_model_light") or "").classes("w-full").props("outlined")
    strong = ui.input("强模型（匹配/润色用）", value=config.get("llm_model_strong") or "").classes("w-full").props("outlined")

    def save():
        config.set_value("llm_base_url", base.value.strip())
        config.set_value("llm_api_key", key.value.strip())
        config.set_value("llm_model_light", light.value.strip())
        config.set_value("llm_model_strong", strong.value.strip())
        ui.notify("已保存", type="positive")

    async def test():
        save()
        client = LLMClient()
        if not client.configured:
            ui.notify("未配置网关：当前走启发式兜底（关键词规则打分/匹配，离线可测）。"
                      "填入 base_url + api_key 后再测，即可切真实 LLM。", type="warning", multi_line=True)
            return
        try:
            await run.io_bound(client.ping)
            ui.notify("连接成功 ✅ 打分/匹配将走真实 LLM", type="positive")
        except Exception as e:
            ui.notify(f"连接失败：{e}", type="negative", multi_line=True, close_button=True)

    with ui.row():
        ui.button("保存", on_click=save).props("color=primary")
        ui.button("保存并测试连接", on_click=test).props("outline")


def _numeric_panel(fields):
    inputs = {}
    for key, label in fields:
        inputs[key] = ui.input(label, value=config.get(key) or "").classes("w-full").props("outlined")

    def save():
        for key, inp in inputs.items():
            config.set_value(key, inp.value.strip())
        ui.notify("已保存", type="positive")
    ui.button("保存", on_click=save).props("color=primary")


# ====================================================================================
# 账号（含凭据）
# ====================================================================================
_OFFICIAL_FIELDS = [
    ("consumer_key", "API Key（Consumer Key）", False),
    ("consumer_secret", "API Key Secret（Consumer Secret）", True),
    ("access_token", "Access Token", False),
    ("access_token_secret", "Access Token Secret", True),
    ("bearer_token", "Bearer Token（选填）", True),
]
_UNOFFICIAL_FIELDS = [
    ("auth_token", "Cookie: auth_token（推荐）", True),
    ("ct0", "Cookie: ct0（推荐）", True),
    ("username", "用户名（不用 Cookie 时填）", False),
    ("email", "邮箱（选填，登录校验用）", False),
    ("password", "密码（不用 Cookie 时填）", True),
    ("totp_secret", "两步验证 TOTP 密钥（开了 2FA 才填）", True),
    ("proxy", "代理（选填，如 http://127.0.0.1:7890）", False),
]


def _accounts_panel():
    ui.label("发帖 / 回复账号管理").classes("font-semibold")
    ui.label("Mock 演示模式下也需要至少一个账号来承载发送。真实模式下：官方通道填 X 开发者平台的密钥"
             "（需 Read and Write 权限），非官方通道填浏览器里登录后的 Cookie。主号只能用官方通道。").classes("text-xs text-gray-400")
    body = ui.column().classes("w-full gap-2")

    def save_account(data: dict, creds: dict, existing_id: int | None = None) -> bool:
        handle = data["handle"].lstrip("@").strip()
        if not handle:
            ui.notify("请填写 handle", type="negative"); return False
        if data["is_primary"] and data["access_type"] == "unofficial":
            ui.notify("主号不能使用非官方（twifork）通道——封号风险过高", type="negative"); return False
        if data["max_interval_sec"] < data["min_interval_sec"]:
            ui.notify("最大间隔需 ≥ 最小间隔", type="negative"); return False
        creds_json = json.dumps({k: v for k, v in creds.items() if (v or "").strip()}, ensure_ascii=False)
        try:
            with get_conn() as conn:
                if data["is_primary"]:  # 主号唯一：先清空其他主号
                    conn.execute("UPDATE accounts SET is_primary=0")
                if existing_id:
                    conn.execute(
                        "UPDATE accounts SET handle=?, display_name=?, access_type=?, is_primary=?, "
                        "credentials=?, daily_post_limit=?, daily_reply_limit=?, "
                        "min_interval_sec=?, max_interval_sec=?, active_hours_start=?, active_hours_end=?, "
                        "timezone=?, note=?, status=CASE WHEN status='auth_error' THEN 'active' ELSE status END WHERE id=?",
                        (handle, data["display_name"], data["access_type"], 1 if data["is_primary"] else 0,
                         creds_json, data["daily_post_limit"], data["daily_reply_limit"],
                         data["min_interval_sec"], data["max_interval_sec"], data["active_start"], data["active_end"],
                         data["timezone"], data["note"], existing_id))
                else:
                    conn.execute(
                        "INSERT INTO accounts(handle, display_name, access_type, is_primary, credentials, "
                        "daily_post_limit, daily_reply_limit, min_interval_sec, max_interval_sec, "
                        "active_hours_start, active_hours_end, timezone, note, created_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (handle, data["display_name"], data["access_type"], 1 if data["is_primary"] else 0,
                         creds_json, data["daily_post_limit"], data["daily_reply_limit"],
                         data["min_interval_sec"], data["max_interval_sec"], data["active_start"], data["active_end"],
                         data["timezone"], data["note"], utcnow_iso()))
                conn.commit()
        except sqlite3.IntegrityError as e:
            ui.notify(f"保存失败：handle 可能重复或违反约束（{e}）", type="negative"); return False
        factory.invalidate()
        ui.notify("已保存", type="positive")
        return True

    def open_dialog(existing: sqlite3.Row | None = None):
        creds = parse_credentials(existing["credentials"]) if existing else {}
        with ui.dialog() as dlg, ui.card().classes("w-[560px] max-w-[95vw]"):
            ui.label("编辑账号" if existing else "添加账号").classes("text-lg font-bold")
            with ui.row().classes("w-full gap-2 no-wrap"):
                handle = ui.input("handle（不含 @）", value=existing["handle"] if existing else "") \
                    .classes("flex-1").props("outlined dense")
                dname = ui.input("显示名", value=existing["display_name"] if existing else "") \
                    .classes("flex-1").props("outlined dense")
            atype = ui.select({"official": "官方 API（可作主号，读写按量计费）", "unofficial": "非官方 twifork（仅小号，Cookie 登录）"},
                              value=existing["access_type"] if existing else "official", label="通道类型") \
                .classes("w-full").props("outlined dense")
            primary = ui.switch("设为主号", value=bool(existing["is_primary"]) if existing else False)

            # ---- 凭据区：按通道类型显示不同字段 ----
            ui.separator()
            ui.label("凭据（保存在本机数据库 data/x_operator.db，不会上传）").classes("font-semibold text-sm")
            cred_inputs: dict[str, ui.input] = {}
            official_box = ui.column().classes("w-full gap-1")
            with official_box:
                ui.label("在 developer.x.com 的 App → Keys and tokens 里生成；App 权限须为 Read and Write，"
                         "改权限后要重新生成 Access Token。").classes("text-xs text-gray-400")
                for k, label, secret in _OFFICIAL_FIELDS:
                    cred_inputs[k] = ui.input(label, value=creds.get(k, ""), password=secret,
                                              password_toggle_button=secret).classes("w-full").props("outlined dense")
            unofficial_box = ui.column().classes("w-full gap-1")
            with unofficial_box:
                ui.label("推荐用 Cookie：浏览器登录该小号 → F12 → Application/存储 → Cookies → x.com，"
                         "复制 auth_token 和 ct0 的值。账号密码登录更容易触发风控，成功后会自动保存 Cookie。").classes("text-xs text-gray-400")
                for k, label, secret in _UNOFFICIAL_FIELDS:
                    cred_inputs[k] = ui.input(label, value=creds.get(k, ""), password=secret,
                                              password_toggle_button=secret).classes("w-full").props("outlined dense")

            def sync_boxes():
                official_box.set_visibility(atype.value == "official")
                unofficial_box.set_visibility(atype.value != "official")
            atype.on("update:model-value", lambda e: sync_boxes())
            sync_boxes()

            # ---- 限速 / 活跃时段 ----
            ui.separator()
            ui.label("限速与活跃时段").classes("font-semibold text-sm")
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
            with ui.row().classes("w-full gap-2 no-wrap"):
                a_start = ui.input("活跃开始 HH:MM", value=existing["active_hours_start"] if existing else "09:00") \
                    .props("outlined dense").classes("flex-1")
                a_end = ui.input("活跃结束 HH:MM", value=existing["active_hours_end"] if existing else "22:00") \
                    .props("outlined dense").classes("flex-1")
                tz_val = existing["timezone"] if existing else "Asia/Tokyo"
                tz_opts = _TZ_OPTIONS if tz_val in _TZ_OPTIONS else [tz_val] + _TZ_OPTIONS
                tz = ui.select(tz_opts, value=tz_val, label="时区").props("outlined dense").classes("flex-1")
            ui.label("活跃时段相同（如 00:00-00:00）表示全天可发。").classes("text-xs text-gray-400")
            note = ui.input("备注", value=existing["note"] if existing else "").classes("w-full").props("outlined dense")

            def collect():
                return {
                    "handle": handle.value or "",
                    "display_name": dname.value or "",
                    "access_type": atype.value,
                    "is_primary": bool(primary.value),
                    "daily_post_limit": int(post_lim.value or 0),
                    "daily_reply_limit": int(reply_lim.value or 0),
                    "min_interval_sec": int(mn.value or 0),
                    "max_interval_sec": int(mx.value or 0),
                    "active_start": (a_start.value or "09:00").strip(),
                    "active_end": (a_end.value or "22:00").strip(),
                    "timezone": tz.value or "Asia/Tokyo",
                    "note": note.value or "",
                }

            def collect_creds():
                keys = [k for k, _, _ in (_OFFICIAL_FIELDS if atype.value == "official" else _UNOFFICIAL_FIELDS)]
                return {k: (cred_inputs[k].value or "").strip() for k in keys}

            def do_save():
                for hhmm in (a_start.value, a_end.value):
                    if not _valid_hhmm(hhmm):
                        ui.notify("活跃时段格式应为 HH:MM", type="negative"); return
                ok = save_account(collect(), collect_creds(), existing["id"] if existing else None)
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

    async def del_account(a):
        if not await confirm(f"删除账号 @{a['handle']}？", "凭据会一并删除。有发送记录/队列/定时计划关联的账号无法删除。"):
            return
        try:
            with get_conn() as conn:
                conn.execute("DELETE FROM accounts WHERE id=?", (a["id"],))
                conn.commit()
            factory.invalidate(a["id"])
            ui.notify("已删除", type="positive")
        except sqlite3.IntegrityError:
            ui.notify("该账号已有关联记录（发送/队列/定时），无法删除；请改为「暂停」", type="negative")
        render()

    async def test_conn(a):
        ok, why = factory.credential_status(a)
        if not ok:
            ui.notify(f"未填凭据（{why}）。" + ("当前 Mock 模式下抓取/发送走假数据，可正常演示。" if config.get_bool("dry_run", True) else ""),
                      type="warning", multi_line=True, close_button=True)
            return
        ui.notify("正在连接 X…", type="info")

        def _probe():
            client = factory.get_real_client(a)
            return client.get_me()
        try:
            user = await run.io_bound(_probe)
        except Exception as e:
            ui.notify(f"连接失败：{e}", type="negative", multi_line=True, close_button=True, timeout=15000)
            return
        with get_conn() as conn:
            conn.execute("UPDATE accounts SET status='active' WHERE id=? AND status='auth_error'", (a["id"],))
            conn.commit()
        factory.invalidate(a["id"])
        ui.notify(f"连接成功 ✅ 凭据对应的账号是 @{user.handle}（{user.display_name}）"
                  + ("" if user.handle.lower() == a["handle"].lower() else f"——注意与你填的 @{a['handle']} 不一致"),
                  type="positive", multi_line=True, close_button=True)
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
                cred_ok, cred_why = factory.credential_status(a)
                with ui.card().classes("w-full"):
                    with ui.row().classes("items-center gap-2"):
                        ui.label(f"@{a['handle']}").classes("font-semibold")
                        if a["display_name"]:
                            ui.label(a["display_name"]).classes("text-xs text-gray-400")
                        ui.badge("官方 API" if a["access_type"] == "official" else "非官方 Cookie").classes("bg-slate-500")
                        if a["is_primary"]:
                            ui.badge("主号 ★").classes("bg-amber-500")
                        ui.badge({"active": "启用", "paused": "已暂停", "auth_error": "凭据失效"}.get(a["status"], a["status"])) \
                            .classes("bg-green-600" if a["status"] == "active" else "bg-red-600")
                        ui.badge("凭据已填" if cred_ok else "未填凭据").classes("bg-emerald-600" if cred_ok else "bg-orange-500").tooltip(cred_why)
                    ui.label(f"日发帖 {a['daily_post_limit']} / 日回复 {a['daily_reply_limit']} · "
                             f"间隔 {a['min_interval_sec']}-{a['max_interval_sec']}s · "
                             f"活跃 {a['active_hours_start']}-{a['active_hours_end']} {a['timezone']}"
                             + (f" · {a['note']}" if a["note"] else "")).classes("text-xs text-gray-400")
                    with ui.row().classes("gap-1 flex-wrap"):
                        ui.button("测试连接", icon="wifi_tethering", on_click=lambda aa=a: test_conn(aa)).props("flat dense")
                        ui.button("编辑 / 填凭据", icon="edit", on_click=lambda aa=a: open_dialog(aa)).props("flat dense")
                        if not a["is_primary"] and a["access_type"] == "official":
                            ui.button("设为主号", on_click=lambda aid=a["id"]: set_primary(aid)).props("flat dense")
                        if a["status"] == "active":
                            ui.button("暂停", on_click=lambda aid=a["id"]: (_set_acc_status(aid, "paused"), render())).props("flat dense")
                        else:
                            ui.button("启用", on_click=lambda aid=a["id"]: (_set_acc_status(aid, "active"), render())).props("flat dense")
                        ui.button("删除", icon="delete", on_click=lambda aa=a: del_account(aa)).props("flat dense color=negative")
    render()


def _valid_hhmm(s: str | None) -> bool:
    try:
        h, m = (s or "").strip().split(":")
        return 0 <= int(h) < 24 and 0 <= int(m) < 60
    except Exception:
        return False


def _set_acc_status(aid, status):
    with get_conn() as conn:
        conn.execute("UPDATE accounts SET status=? WHERE id=?", (status, aid))
        conn.commit()
    factory.invalidate(aid)


# ====================================================================================
# 黑名单
# ====================================================================================
def _blacklist_panel():
    ui.label("黑名单里的作者不会被回复；可填 @handle 或 X 的数字 user_id。").classes("text-xs text-gray-400")
    with ui.row().classes("items-end gap-2"):
        inp = ui.input("@handle 或 user_id").props("outlined dense")
        reason_in = ui.input("原因（选填）").props("outlined dense")
        add_btn = ui.button("添加", icon="add").props("color=primary")
    body = ui.column().classes("w-full gap-1")

    def add():
        v = (inp.value or "").strip().lstrip("@")
        if not v:
            return
        with get_conn() as conn:
            conn.execute("INSERT INTO blacklist(x_user_id, handle, reason, created_at) VALUES (?,?,?,?) "
                         "ON CONFLICT(x_user_id) DO NOTHING", (v, v, (reason_in.value or "手动添加").strip(), utcnow_iso()))
            conn.commit()
        inp.value = ""; reason_in.value = ""; render(); ui.notify("已添加", type="positive")
    add_btn.on_click(add)

    def render():
        body.clear()
        with get_conn() as conn:
            rows = conn.execute("SELECT * FROM blacklist ORDER BY id DESC").fetchall()
        with body:
            if not rows:
                ui.label("黑名单为空").classes("text-gray-400")
            for b in rows:
                with ui.row().classes("items-center gap-2"):
                    ui.label(f"@{b['handle']}" if b["handle"] else b["x_user_id"]).classes("text-sm")
                    if b["handle"] and b["handle"] != b["x_user_id"]:
                        ui.label(f"id {b['x_user_id']}").classes("text-xs text-gray-400")
                    ui.label(b["reason"]).classes("text-xs text-gray-400")
                    ui.button("移除", on_click=lambda bb=b: (_del_bl(bb["id"]), render())).props("flat dense color=negative")
    render()


def _del_bl(bid):
    with get_conn() as conn:
        conn.execute("DELETE FROM blacklist WHERE id=?", (bid,))
        conn.commit()


# ====================================================================================
# 数据清理
# ====================================================================================
def _data_panel():
    ui.label("数据清理").classes("font-semibold")
    ui.label("从演示切到真实使用时，可以把演示期间产生的抓取记录和审核队列一键清掉；"
             "素材、账号、规则、黑名单、去重账本都会保留。").classes("text-xs text-gray-400")
    info = ui.label("").classes("text-sm")

    def refresh_info():
        with get_conn() as conn:
            t = conn.execute("SELECT COUNT(*) AS c FROM target_tweets").fetchone()["c"]
            q = conn.execute("SELECT COUNT(*) AS c FROM review_queue").fetchone()["c"]
            i = conn.execute("SELECT COUNT(*) AS c FROM interactions").fetchone()["c"]
            m = conn.execute("SELECT COUNT(*) AS c FROM target_tweets WHERE author_id LIKE 'mock_user_%'").fetchone()["c"]
        info.text = f"当前：抓取记录 {t} 条（其中演示数据 {m} 条）· 审核队列 {q} 条 · 去重账本 {i} 条"
    refresh_info()

    async def clear_demo():
        if await confirm("清除演示数据？", "删除所有 Mock 演示模式产生的抓取记录、对应队列条目和去重账本记录。真实数据不受影响。", ok_label="清除"):
            with get_conn() as conn:
                conn.execute("DELETE FROM review_queue WHERE target_tweet_id IN (SELECT id FROM target_tweets WHERE author_id LIKE 'mock_user_%')")
                conn.execute("DELETE FROM review_queue WHERE sent_tweet_id LIKE 'mock_%'")
                conn.execute("DELETE FROM target_tweets WHERE author_id LIKE 'mock_user_%'")
                conn.execute("DELETE FROM interactions WHERE author_id LIKE 'mock_user_%' OR tweet_id LIKE 'mock_%'")
                conn.execute("DELETE FROM action_log WHERE api_kind='x_mock'")
                conn.commit()
            ui.notify("演示数据已清除", type="positive"); refresh_info()

    async def clear_all():
        if await confirm("清空全部抓取记录与审核队列？", "包括真实数据。去重账本（防止重复回复）会保留。", ok_label="全部清空"):
            with get_conn() as conn:
                conn.execute("DELETE FROM review_queue")
                conn.execute("DELETE FROM target_tweets")
                conn.execute("UPDATE watched_users SET last_seen_tweet_id=NULL, hit_count=0")
                conn.execute("UPDATE search_rules SET newest_id_cursor=NULL")
                conn.commit()
            ui.notify("已清空", type="positive"); refresh_info()

    with ui.row().classes("gap-2"):
        ui.button("清除演示数据", icon="cleaning_services", on_click=clear_demo).props("outline")
        ui.button("清空全部抓取记录与审核队列", icon="delete_forever", on_click=clear_all).props("outline color=negative")
