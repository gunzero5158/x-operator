"""设置（design-v1.1 §8.7）：运行模式、LLM、合规参数、预算、账号（含凭据）、黑名单、数据清理。"""
from __future__ import annotations

import json
import sqlite3

from nicegui import run, ui

from .. import config
from ..adapters import factory
from ..adapters.real import (OFFICIAL_REQUIRED, describe_proxy, detect_system_proxy,
                             parse_credentials, validate_unofficial_credentials)
from ..db.database import get_conn, utcnow_iso
from ..llm.client import (SCENE_TIERS, TIER_DEFAULT_MODEL, TIER_LABEL, TIER_SETTING_KEY,
                          LLMClient)
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
                t_run = ui.tab("自动运行")
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
                    ui.label("这些是所有规则/推主共用的安全阀。抓取时间窗已经放到每条搜索规则、每个监控推主自己的设置里（「首次回溯」）。").classes("text-xs text-gray-400")
                    _numeric_panel([
                        ("cooldown_days", "作者冷却天数",
                         "对同一个作者两次互动之间至少隔几天，避免被对方和 X 当成骚扰。推荐 7；养号期小号 14。", int, 0, 365),
                        ("reply_ttl_hours", "回复条目时效（小时）",
                         "待审核条目超过这个时长自动过期——回复太旧的推文没意义还显得像机器人。推荐 48；热点类 24。", int, 1, 720),
                        ("match_confidence_threshold", "素材匹配置信度阈值（0-1）",
                         "AI 匹配素材时的信心低于此值，就不用 AI 选的那条，改为按规则给一条用得最少的素材（理由里会写明，照样进待审核，不会空手）。"
                         "推荐 0.4；想让 AI 的选择更多被采纳就填 0.3。", float, 0, 1),
                        ("grace_period_hours", "定时补发宽限（小时）",
                         "程序没开着导致定时计划错过了，重启后如果错过还不到这么多小时就照常补发；超过就标为「错过」不补（周期计划跳到下一次）。推荐 2。", int, 0, 168),
                        ("nurture_days", "养号期天数",
                         "新添加的小号在这么多天内日限额自动减半。推荐 14；老号可填 0。", int, 0, 365),
                    ])
                with ui.tab_panel(t_budget):
                    ui.label("读额度只统计官方 API 通道的读取（X 只对它按条计费；小号 Cookie 通道免费、不占额度）。"
                             "怎么数：每次抓取按 X 返回的推文条数记（被过滤掉的也算，开了观看量门槛时按扫描条数记），发送后回查算 1 条，发送本身不占读额度。"
                             "真的会拦：自动轮询在「剩余 ≤ 熔断保留」时停跑；手动运行在额度用完时拒绝。每天 0 点（UTC）重置。"
                             "这是程序自己的估算，不是 X 的账单；实际费用以开发者后台为准。").classes("text-xs text-gray-400")
                    _numeric_panel([
                        ("daily_read_budget", "每日读额度（条）",
                         "每天最多从官方 API 读多少条推文（监控+搜索+回查合计），防止账单失控。按量计费约 $0.005/条：330 条 ≈ $1.6/天。填 0 = 不限。", int, 0, 1000000),
                        ("budget_reserve_reads", "熔断保留读额度",
                         "读额度只剩这么多时停止自动轮询，留给手动操作。推荐 20。", int, 0, 100000),
                        ("monthly_budget_usd", "月预算（USD）",
                         "仪表盘按本月读取量估算官方 API 读费用并与它对照（约 $0.005/条；发推另计）。", float, 0, 1000000),
                        ("monitor_interval_minutes", "自动监控间隔（分钟）",
                         "开了后台自动轮询时，多久跑一次监控。推荐 50~120；改后需重启生效。", int, 1, 1440),
                        ("search_runs_per_day", "每日自动搜索次数",
                         "开了后台自动轮询时，一天跑几次搜索（间隔 = 24 小时 ÷ 次数，最短 30 分钟）。推荐 2~4；改后需重启生效。", int, 1, 48),
                    ])
                with ui.tab_panel(t_bl):
                    _blacklist_panel()
                with ui.tab_panel(t_data):
                    _data_panel()


def _run_panel():
    ui.label("自动运行").classes("font-semibold")
    ui.label("所有抓取和发送都用「账号」里填的真实凭据直连 X，没有演示/模拟模式。").classes("text-xs text-gray-400")
    auto = ui.switch("启用后台自动轮询（监控/搜索按间隔自动跑）", value=config.get_bool("auto_jobs_enabled", False))
    auto.on("update:model-value", lambda e: (config.set_value("auto_jobs_enabled", bool(e.args)),
                                             ui.notify("已更新自动轮询开关", type="positive")))
    ui.label("测试期建议保持关闭，用各页面的「运行一次」按钮手动触发，便于观察每一步。"
             "无论开关如何，「定时计划到点检查」和「发送分发」每分钟都会自动跑。").classes("text-xs text-gray-400")


def _llm_panel():
    ui.label("LLM 网关（OpenAI 兼容；留空则用启发式兜底，离线可测）").classes("font-semibold")
    base = ui.input("base_url（例：https://api.openai.com/v1，或你用的中转站地址）", value=config.get("llm_base_url") or "").classes("w-full").props("outlined")
    key = ui.input("api_key", value=config.get("llm_api_key") or "", password=True, password_toggle_button=True).classes("w-full").props("outlined")
    light = ui.input("轻量模型（便宜、快；量大判断简单的任务）", value=config.get("llm_model_light") or "").classes("w-full").props("outlined")
    strong = ui.input("强模型（要写东西、要做取舍的任务）", value=config.get("llm_model_strong") or "").classes("w-full").props("outlined")
    ui.label("哪些任务用哪档模型（对照表登记在代码 x_operator/llm/client.py 的 SCENE_TIERS；新功能必须先登记才能调用，所以这张表永远是最新的）：").classes("text-xs text-gray-500 mt-2")
    tier_rows = [{"任务": desc, "模型档": TIER_LABEL[tier],
                  "当前模型": (config.get(TIER_SETTING_KEY[tier]) or TIER_DEFAULT_MODEL[tier])}
                 for scene, (tier, desc) in SCENE_TIERS.items()]
    ui.table(columns=[{"name": k, "label": k, "field": k, "align": "left"} for k in ("任务", "模型档", "当前模型")],
             rows=tier_rows).classes("w-full").props("dense flat bordered wrap-cells")

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
    """fields：(key, 标签, 说明, 类型 int/float, 最小, 最大)。保存前逐项校验，填错哪个就提示哪个，全部合格才写库。"""
    inputs = {}
    for key, label, hint, _kind, lo, hi in fields:
        inputs[key] = ui.input(label, value=config.get(key) or "").classes("w-full").props("outlined")
        ui.label(hint + f"（允许范围 {lo:g}~{hi:g}）").classes("text-xs text-gray-400 -mt-2 mb-2")

    def save():
        parsed = {}
        for key, label, _hint, kind, lo, hi in fields:
            raw = (inputs[key].value or "").strip()
            try:
                val = kind(float(raw)) if kind is int else kind(raw)
                if kind is int and float(raw) != val:
                    raise ValueError
            except (TypeError, ValueError):
                ui.notify(f"「{label}」要填{'整数' if kind is int else '数字'}，现在填的是「{raw}」", type="negative"); return
            if not (lo <= val <= hi):
                ui.notify(f"「{label}」要在 {lo:g}~{hi:g} 之间，现在填的是 {val:g}", type="negative"); return
            parsed[key] = val
        for key, val in parsed.items():
            config.set_value(key, val)
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
    ("proxy", "代理（留空=自动用系统代理；填 direct 强制直连）", False),
]
_UNOFFICIAL_FIELDS = [
    ("auth_token", "Cookie: auth_token（方式一）", True),
    ("ct0", "Cookie: ct0（方式一）", True),
    ("username", "用户名 @handle 或邮箱（方式二）", False),
    ("password", "密码（方式二）", True),
    ("totp_secret", "两步验证 TOTP 密钥（方式二，开了 2FA 必填）", True),
    ("email", "邮箱（方式二选填：X 要求二次确认身份时用）", False),
    ("proxy", "代理（留空=自动用系统代理；填 direct 强制直连）", False),
]

_COOKIE_GUIDE = """
**方式一：从浏览器复制 Cookie（最稳，推荐）**

1. 用 **Chrome 或 Edge** 打开 <https://x.com>，登录**要绑定的这个小号**（注意别登错号）。
2. 登录后停在 x.com 任意页面，按键盘 **F12**（或 Ctrl+Shift+I）打开「开发者工具」，一般在页面右侧或下方弹出。
3. 在开发者工具**顶部的一排标签**里找 **「Application」**（中文界面叫 **「应用」或「应用程序」**）。
   看不到的话点标签栏最右边的 **»** 展开，里面就有。
4. 左侧栏找 **Storage / 存储** 下面的 **「Cookies」**，点它左边的小三角展开，点 **https://x.com**。
5. 右侧出现一张表。在 **Name（名称）** 列里找到 **auth_token** 这一行
   （可以在表格上方的 Filter 搜索框输入 auth_token 快速找到）。
6. **双击这一行的 Value（值）** 那一格 → 文字全选变蓝 → **Ctrl+C** 复制 → 粘贴到下面「auth_token」框里。
   它是 **40 位**由数字和小写字母 a-f 组成的字符串。
7. 同样方法找到 **ct0**，复制它的 Value 粘贴到「ct0」框。它比较长（**32 位或 160 位**）。
8. 点「保存」→ 回到账号卡片点「测试连接」，显示 ✅ 和账号名就说明绑定成功。

注意：
- **不要在浏览器里点「退出登录」**，一退出 auth_token 就作废；直接关掉标签页即可。
- Cookie 一般能用几个月；失效时「测试连接」会提示，重新复制一次即可。如果同时填了方式二，会自动重新登录。
- 用 Firefox 的话：F12 → **存储（Storage）** 标签 → Cookie → https://x.com，其余相同。

**方式二：用户名 + 密码 + 两步验证密钥（自动登录，Cookie 失效时也能自己续）**

- 「用户名」填 @handle（不带 @）或登录邮箱；「密码」填登录密码。
- 账号开了两步验证（2FA）的，「TOTP 密钥」**必填**：就是当初在 X「设置 → 安全性 → 两步验证 → 身份验证应用」
  绑定验证器时给你的那串 **16~32 位字母数字密钥**（扫码页面上一般有「无法扫码？手动输入密钥」）。
  不是验证器 App 里每 30 秒变化的 6 位数字。
- 如果 X 登录时额外弹「请输入邮箱/手机号确认身份」，把「邮箱」也填上就能自动过。
- 本工具**不支持邮箱验证码**：如果 X 坚持要邮箱验证码，会弹出明确提示——先在浏览器登录一次该账号完成验证，
  之后再试，或改用方式一。
- 登录成功后会自动把 Cookie 存到方式一的两个框里，之后都走 Cookie，不会反复登录。

**代理**：留空时自动使用电脑当前的系统代理（Windows「设置 → 网络和 Internet → 代理」里开着的那个，
或环境变量 HTTP_PROXY）。想指定就填 `http://127.0.0.1:7890` 这种；填 `direct` 表示强制直连。
"""

_OFFICIAL_GUIDE = """
1. 打开 <https://developer.x.com>，用**这个账号**登录，进入 **Developer Portal**（首次需要注册开发者并同意条款，
   按量付费需要绑卡）。
2. 左侧 **Projects & Apps** → 打开你的 App（没有就 **+ Add App** 新建一个）。
3. App 页面点 **Settings（齿轮）** → 找到 **User authentication settings** → **Set up / Edit**：
   - **App permissions** 选 **Read and write**（默认是只读，只读发不了推）
   - **Type of App** 选 **Web App, Automated App or Bot**
   - **Callback URI** 和 **Website URL** 随便填一个合法网址（如 `http://localhost` / `https://example.com`），保存。
4. 回到 App 页面点 **Keys and tokens** 标签：
   - **Consumer Keys → API Key and Secret** 点 **Regenerate**，把 API Key、API Key Secret 分别复制到下面前两个框。
   - **Authentication Tokens → Access Token and Secret** 点 **Regenerate**，复制 Access Token、Access Token Secret 到第三、四个框。
     生成后页面上应显示 **Created with Read and Write permissions**——如果显示 Read only，说明第 3 步权限没保存，改完要**重新生成**这一对。
   - Bearer Token 可填可不填。
5. 这些密钥只在生成时显示一次，关掉就看不到了（可以再 Regenerate）。填好点「保存」→「测试连接」。
"""


def _accounts_panel():
    ui.label("发帖 / 回复账号管理").classes("font-semibold")
    ui.label("官方通道填 X 开发者平台的密钥（需 Read and Write 权限）；非官方通道填浏览器 Cookie，或用户名+密码+两步验证密钥。"
             "弹窗里有手把手的获取步骤。填好后务必点「测试连接」。").classes("text-xs text-gray-400")
    ui.label("多账号分工：「主号」（弹窗里的「设为主号」开关，只有官方 API 通道能当主号）负责抓取；回复默认在启用中的小号里自动轮流、"
             "主号不参与（一个小号都没有时才用主号）；每条搜索规则/监控推主可指定固定的回复账号，审核队列里每条也能临时改。"
             "发帖的账号在定时计划里选。").classes("text-xs text-gray-400")
    sys_proxy = detect_system_proxy()
    ui.label("本机系统代理：" + (sys_proxy if sys_proxy else "未检测到（将直连）") +
             "。账号里代理留空时自动使用它。").classes("text-xs text-gray-400")
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
        with ui.dialog() as dlg, ui.card().classes("w-[680px] max-w-[95vw] max-h-[92vh] overflow-auto"):
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
                with ui.expansion("怎么拿到这 4 个密钥？（点开看步骤）", icon="help_outline").classes("w-full text-sm bg-blue-50 rounded"):
                    ui.markdown(_OFFICIAL_GUIDE).classes("text-xs")
                for k, label, secret in _OFFICIAL_FIELDS:
                    cred_inputs[k] = ui.input(label, value=creds.get(k, ""), password=secret,
                                              password_toggle_button=secret).classes("w-full").props("outlined dense")
            unofficial_box = ui.column().classes("w-full gap-1")
            with unofficial_box:
                with ui.expansion("怎么拿到 auth_token / ct0？密码 + 两步验证怎么填？（点开看手把手步骤）",
                                  icon="help_outline").classes("w-full text-sm bg-blue-50 rounded"):
                    ui.markdown(_COOKIE_GUIDE).classes("text-xs")
                ui.label("方式一（Cookie）和方式二（密码+2FA）填一种即可；两种都填时优先用 Cookie，Cookie 失效自动用方式二重新登录。"
                         ).classes("text-xs text-gray-500")
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
            ui.label("日发帖/日回复上限：每天最多发几条，超了自动等明天。推荐主号 5/10、小号 3/5，养号期减半。"
                     "间隔：两次发送之间随机等待的秒数范围，越像人越安全。推荐 180~600（3~10 分钟），小号 600~1800。"
                     "活跃时段：只在这个时段内发送（按所选时区），模拟真人作息；首尾相同（如 00:00-00:00）表示全天。推荐 09:00-22:00。"
                     ).classes("text-xs text-gray-400")
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
                creds_now = collect_creds()
                if atype.value != "official":
                    problem = validate_unofficial_credentials(creds_now)
                    if problem:
                        ui.notify(problem, type="negative", multi_line=True, close_button=True, timeout=15000); return
                    if (creds_now.get("username") and not creds_now.get("password")) or \
                       (creds_now.get("password") and not creds_now.get("username")):
                        ui.notify("方式二需要用户名和密码都填", type="negative"); return
                ok = save_account(collect(), creds_now, existing["id"] if existing else None)
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
            ui.notify(f"未填凭据（{why}）。点「编辑 / 填凭据」补上。", type="warning", multi_line=True, close_button=True)
            return
        ui.notify("正在连接 X…（密码登录可能要 10~30 秒）", type="info")

        def _probe():
            client = factory.get_real_client(a)
            return client.get_me(), getattr(client, "proxy_used", None)
        try:
            user, proxy = await run.io_bound(_probe)
        except Exception as e:
            ui.notify(f"连接失败：{e}", type="negative", multi_line=True, close_button=True, timeout=20000)
            return
        with get_conn() as conn:
            conn.execute("UPDATE accounts SET status='active' WHERE id=? AND status='auth_error'", (a["id"],))
            conn.commit()
        factory.invalidate(a["id"])
        ui.notify(f"连接成功 ✅ 凭据对应的账号是 @{user.handle}（{user.display_name}），{describe_proxy(proxy)}"
                  + ("" if user.handle.lower() == a["handle"].lower() else f"——注意与你填的 @{a['handle']} 不一致"),
                  type="positive", multi_line=True, close_button=True, timeout=12000)
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
def _resolve_blacklist_entry(raw: str) -> tuple[str, str, str]:
    """把用户输入变成 (x_user_id, handle, 备注)。填 @handle 时通过账号去 X 查数字 id（拦截时两者都会比对；
    查不到也照样按 handle 存，只是提示一下）。阻塞：在线程池里跑。"""
    from ..core.monitor import get_primary_account
    v = raw.strip().lstrip("@")
    if v.isdigit():
        return v, "", ""
    account = get_primary_account()
    if account is None:
        return v, v, "（没有启用的账号，无法查数字 id；按 @handle 拦截）"
    try:
        user = factory.get_client(account).get_user_by_handle(v)
        return user.user_id, user.handle, ""
    except Exception as e:
        return v, v, f"（查数字 id 失败：{str(e)[:60]}；按 @handle 拦截）"


def _blacklist_panel():
    ui.label("黑名单里的作者不会被回复（监控/搜索预检和发送前都会拦）；可填 @handle 或 X 的数字 user_id，两者都能匹配。").classes("text-xs text-gray-400")
    with ui.row().classes("items-end gap-2"):
        inp = ui.input("@handle 或 user_id").props("outlined dense")
        reason_in = ui.input("原因（选填）").props("outlined dense")
        add_btn = ui.button("添加", icon="add").props("color=primary")
    body = ui.column().classes("w-full gap-1")

    async def add():
        v = (inp.value or "").strip().lstrip("@")
        if not v:
            return
        add_btn.disable()
        try:
            uid, handle, note = await run.io_bound(_resolve_blacklist_entry, v)
        finally:
            add_btn.enable()
        with get_conn() as conn:
            conn.execute("INSERT INTO blacklist(x_user_id, handle, reason, created_at) VALUES (?,?,?,?) "
                         "ON CONFLICT(x_user_id) DO UPDATE SET handle=CASE WHEN excluded.handle<>'' THEN excluded.handle ELSE handle END",
                         (uid, handle, (reason_in.value or "手动添加").strip(), utcnow_iso()))
            conn.commit()
        inp.value = ""; reason_in.value = ""; render()
        ui.notify("已添加" + note, type="positive" if not note else "warning", multi_line=True)
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
    ui.label("测试期可以把抓取记录和审核队列一键清掉重来；素材、账号、规则、黑名单、去重账本都会保留。").classes("text-xs text-gray-400")
    info = ui.label("").classes("text-sm")

    def refresh_info():
        with get_conn() as conn:
            t = conn.execute("SELECT COUNT(*) AS c FROM target_tweets").fetchone()["c"]
            q = conn.execute("SELECT COUNT(*) AS c FROM review_queue").fetchone()["c"]
            i = conn.execute("SELECT COUNT(*) AS c FROM interactions").fetchone()["c"]
        info.text = f"当前：抓取记录 {t} 条 · 审核队列 {q} 条 · 去重账本 {i} 条"
    refresh_info()

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
        ui.button("清空全部抓取记录与审核队列", icon="delete_forever", on_click=clear_all).props("outline color=negative")
