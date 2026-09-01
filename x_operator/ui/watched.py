"""监控推主（design-v1.1 §8.4）：添加/编辑/删除/启停被监控的推主，重置游标，运行一次监控。

每个推主可单独设：首次回溯时间窗、含不含回复、回复方式（匹配素材 / AI 创作 / 只抓取）、AI 创作要求。
"""
from __future__ import annotations

from nicegui import run, ui

from ..adapters import factory
from ..adapters.base import XClientError
from ..core.matcher import REPLY_MODE_LABEL
from ..core.monitor import get_primary_account, is_demo_id
from ..db.database import get_conn, utcnow_iso
from .layout import confirm, fmt_time, run_job, shell

HINTS = {
    "lookback": "第一次监控（或重置游标后）往回看多少小时内的推文；之后每次只看上次之后的新推文。推荐 24；发帖少的博主可 72~168。",
    "include": "开=连他回复别人的推文也监控；关=只看他自己发的主推。推荐关（回复通常没上下文，不适合再回复）。",
    "reply_mode": "抓到新推文后怎么生成回复：匹配素材库=从你的回复素材里选；AI 按要求创作=每条现写（需 LLM）；只抓取=你手动处理。",
    "ai_brief": "给 AI 的创作要求：主题/立场、必须带的链接或 @账号（写在这里会强制原样出现）、语气。",
    "polish": "开=允许 AI 轻微改写素材以衔接对方的话；关=素材原文一字不改。推荐关。",
}


def _hint(key: str):
    ui.label(HINTS[key]).classes("text-xs text-gray-400 -mt-2 mb-1")


def _resolve_and_add(handle: str, note: str) -> str:
    """解析 @handle → x_user_id 并入库。返回错误文案，空串表示成功。（阻塞：在线程池里跑）"""
    handle = handle.lstrip("@").strip()
    if not handle:
        return "请输入 @handle"
    account = get_primary_account()
    if account is None:
        return "没有状态为「启用」的账号，无法解析用户。请先到「设置 → 账号」添加"
    try:
        client = factory.get_client(account)
        user = client.get_user_by_handle(handle)
    except (XClientError, ValueError) as e:
        return f"解析 @{handle} 失败：{e}"
    with get_conn() as conn:
        try:
            conn.execute("INSERT INTO watched_users(handle, x_user_id, note, created_at) VALUES (?,?,?,?)",
                         (user.handle, user.user_id, note.strip(), utcnow_iso()))
            conn.commit()
        except Exception:
            return f"@{user.handle} 已在监控列表中"
    return ""


def register(jobs) -> None:
    @ui.page("/watched")
    def watched_page():
        with shell("/watched"):
            with ui.row().classes("items-center justify-between w-full"):
                ui.label("监控推主").classes("text-2xl font-bold")
                ui.button("运行一次监控", icon="play_arrow",
                          on_click=lambda: run_job(jobs.monitor.run_once, "监控", render,
                                                   result_link=("查看抓取记录", "/targets?source=monitor"))).props("outline")
            ui.label("添加时会通过你的账号去 X 查询该用户；每次运行监控拉取其新推文 → 预检 → 按该推主的「回复方式」生成草稿 → 进审核队列。"
                     " 抓到的推文（含被过滤的及原因）都在「抓取记录」页。新添加的推主默认：首次回溯 24 小时、不含回复、匹配素材库——点「编辑」可改。"
                     ).classes("text-xs text-gray-400")

            with ui.card().classes("w-full"):
                with ui.row().classes("items-end gap-2 w-full"):
                    inp = ui.input("@handle").props("outlined dense")
                    note_in = ui.input("备注（选填）").props("outlined dense").classes("flex-1")

                    async def add():
                        h = inp.value or ""
                        add_btn.disable()
                        try:
                            err = await run.io_bound(_resolve_and_add, h, note_in.value or "")
                        finally:
                            add_btn.enable()
                        if err:
                            ui.notify(err, type="negative", multi_line=True, close_button=True)
                        else:
                            ui.notify("已添加（可点「编辑」调整时间窗和回复方式）", type="positive"); inp.value = ""; note_in.value = ""; render()
                    add_btn = ui.button("添加", icon="add", on_click=add).props("color=primary")

            body = ui.column().classes("w-full gap-2")

            async def delete(u):
                if await confirm(f"删除监控推主 @{u['handle']}？", "已抓取的记录会保留。"):
                    _delete(u["id"]); ui.notify("已删除", type="positive"); render()

            def render():
                body.clear()
                with get_conn() as conn:
                    rows = conn.execute("SELECT * FROM watched_users ORDER BY id").fetchall()
                with body:
                    if not rows:
                        ui.label("暂无监控推主").classes("text-gray-400")
                        return
                    for u in rows:
                        with ui.card().classes("w-full"):
                            with ui.row().classes("items-center justify-between w-full"):
                                with ui.column().classes("gap-0"):
                                    with ui.row().classes("items-center gap-2"):
                                        ui.label(f"@{u['handle']}").classes("font-semibold")
                                        if is_demo_id(u["x_user_id"]):
                                            ui.badge("旧版演示数据").classes("bg-amber-500").tooltip("旧版本留下的假推主，监控会跳过；请删掉后重新添加")
                                        if u["include_replies"]:
                                            ui.badge("含回复").classes("bg-slate-500")
                                        ui.badge(f"首次回溯 {u['lookback_hours']}h").classes("bg-slate-400")
                                        ui.badge(REPLY_MODE_LABEL.get(u["reply_mode"], u["reply_mode"])).classes(
                                            "bg-purple-600" if u["reply_mode"] == "ai_write" else "bg-teal-600")
                                        if not u["enabled"]:
                                            ui.badge("已停用").classes("bg-gray-400")
                                    ui.label(f"命中 {u['hit_count']} 次 · 游标 {u['last_seen_tweet_id'] or '无（下次按首次回溯抓）'}"
                                             + f" · 添加于 {fmt_time(u['created_at'])}"
                                             + (f" · {u['note']}" if u['note'] else "")).classes("text-xs text-gray-400")
                                    if u["reply_mode"] == "ai_write":
                                        ui.label("创作要求：" + (u["ai_brief"] or "（未填！AI 无法创作）")).classes(
                                            "text-xs " + ("text-gray-500" if u["ai_brief"] else "text-red-500"))
                                with ui.row().classes("items-center gap-1"):
                                    sw = ui.switch("启用", value=bool(u["enabled"]))
                                    sw.on("update:model-value", lambda e, uid=u["id"]: _toggle(uid, e.args))
                                    ui.button("查看结果", icon="travel_explore",
                                              on_click=lambda: ui.navigate.to("/targets?source=monitor")).props("flat dense color=primary")
                                    ui.button("编辑", on_click=lambda uu=u: _edit(uu, render)).props("flat dense")
                                    ui.button("重置游标", on_click=lambda uid=u["id"]: (_reset_cursor(uid), ui.notify("已重置，下次监控按「首次回溯」小时数重新抓", type="info"), render())).props("flat dense").tooltip("清掉「上次看到哪条」的记录")
                                    ui.button("删除", icon="delete", on_click=lambda uu=u: delete(uu)).props("flat dense color=negative")

            render()

    def _edit(u, refresh):
        with ui.dialog() as dialog, ui.card().classes("w-[640px] max-w-[95vw] max-h-[92vh] overflow-auto"):
            ui.label(f"编辑 @{u['handle']}").classes("text-lg font-bold")
            note = ui.input("备注", value=u["note"] or "").classes("w-full").props("outlined")
            lookback = ui.number("首次回溯（小时）", value=u["lookback_hours"], min=1, max=720, step=1).classes("w-full").props("outlined")
            _hint("lookback")
            incl = ui.switch("监控时包含其回复", value=bool(u["include_replies"]))
            _hint("include")
            ui.separator()
            ui.label("回复方式").classes("font-semibold text-sm")
            mode = ui.select(REPLY_MODE_LABEL, value=u["reply_mode"], label="抓到新推文后").classes("w-full").props("outlined")
            _hint("reply_mode")
            brief = ui.textarea("AI 创作要求", value=u["ai_brief"] or "").classes("w-full").props("outlined autogrow")
            brief_hint = ui.label(HINTS["ai_brief"]).classes("text-xs text-gray-400 -mt-2 mb-1")
            polish = ui.switch("允许 AI 轻微润色素材", value=bool(u["allow_polish"]))
            polish_hint = ui.label(HINTS["polish"]).classes("text-xs text-gray-400 -mt-2 mb-1")

            def sync():
                is_ai = mode.value == "ai_write"
                brief.set_visibility(is_ai); brief_hint.set_visibility(is_ai)
                polish.set_visibility(mode.value == "material"); polish_hint.set_visibility(mode.value == "material")
            mode.on("update:model-value", lambda e: sync()); sync()

            def save():
                if mode.value == "ai_write" and not (brief.value or "").strip():
                    ui.notify("选了「AI 按要求创作」就必须填创作要求", type="negative"); return
                with get_conn() as conn:
                    conn.execute("UPDATE watched_users SET note=?, include_replies=?, lookback_hours=?, reply_mode=?, ai_brief=?, allow_polish=? WHERE id=?",
                                 ((note.value or "").strip(), 1 if incl.value else 0, int(lookback.value or 24), mode.value,
                                  (brief.value or "").strip(), 1 if polish.value else 0, u["id"]))
                    conn.commit()
                dialog.close(); refresh(); ui.notify("已保存", type="positive")

            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("取消", on_click=dialog.close).props("flat")
                ui.button("保存", on_click=save).props("color=primary")
        dialog.open()


def _toggle(uid: int, enabled):
    with get_conn() as conn:
        conn.execute("UPDATE watched_users SET enabled=? WHERE id=?", (1 if enabled else 0, uid))
        conn.commit()


def _reset_cursor(uid: int):
    with get_conn() as conn:
        conn.execute("UPDATE watched_users SET last_seen_tweet_id=NULL WHERE id=?", (uid,))
        conn.commit()


def _delete(uid: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM watched_users WHERE id=?", (uid,))
        conn.commit()
