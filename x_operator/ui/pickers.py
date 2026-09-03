"""各页共用的两个弹窗：手动选素材、AI 按要求撰写。抓取记录页与审核队列页都会用到。"""
from __future__ import annotations

from nicegui import run, ui

from ..db.database import get_conn
from ..core.matcher import REPLY_MODE_LABEL, extract_must_include
from .layout import notify_long

LANG_NAME = {"ja": "日语", "en": "英语", "zh": "中文", "ko": "韩语", "und": "未知"}

# 搜索规则 / 监控推主共用的「回复方式」三个字段及其说明
REPLY_HINTS = {
    "reply_mode": "抓到达标推文后怎么生成回复：匹配素材库=从你写好的回复素材里选（可控、省钱）；AI 按要求创作=每条现写（更贴合、需 LLM）；"
                  "只抓取=不自动生成，你在抓取记录里逐条手动选素材或让 AI 写。",
    "ai_brief": "给 AI 的创作要求：主题/立场、必须带的链接或 @账号（直接写在这里，会强制原样出现）、语气。例：我们做按量计费的多模型 API 网关，"
                "回复要先接对方的话再提一句，像同行聊天，结尾带 @ApiMaxJP。",
    "polish": "开=允许 AI 在素材基础上轻微改写以衔接对方的话（不改核心信息和链接/@）；关=一字不改用素材原文。推荐关，除非素材是通用模板。",
}


def hint(text: str):
    ui.label(text).classes("text-xs text-gray-400 -mt-2 mb-1")


def reply_mode_fields(mode_value: str, brief_value: str, polish_value: bool, mode_label: str):
    """画出「回复方式 / AI 创作要求 / 允许润色」三个控件（带说明、按模式显隐），返回 (mode, brief, polish)。"""
    ui.separator()
    ui.label("回复方式").classes("font-semibold text-sm")
    mode = ui.select(REPLY_MODE_LABEL, value=mode_value if mode_value in REPLY_MODE_LABEL else "material",
                     label=mode_label).classes("w-full").props("outlined")
    hint(REPLY_HINTS["reply_mode"])
    brief = ui.textarea("AI 创作要求", value=brief_value or "").classes("w-full").props("outlined autogrow")
    brief_hint = ui.label(REPLY_HINTS["ai_brief"]).classes("text-xs text-gray-400 -mt-2 mb-1")
    polish = ui.switch("允许 AI 轻微润色素材", value=bool(polish_value))
    polish_hint = ui.label(REPLY_HINTS["polish"]).classes("text-xs text-gray-400 -mt-2 mb-1")

    def sync():
        is_ai = mode.value == "ai_write"
        brief.set_visibility(is_ai); brief_hint.set_visibility(is_ai)
        polish.set_visibility(mode.value == "material"); polish_hint.set_visibility(mode.value == "material")
    mode.on("update:model-value", lambda e: sync()); sync()
    return mode, brief, polish


def reply_mode_invalid(mode, brief) -> str:
    """保存前校验，返回中文错误（空串 = 没问题）。"""
    if mode.value == "ai_write" and not (brief.value or "").strip():
        return "选了「AI 按要求创作」就必须填创作要求"
    return ""


def _load_materials(lang: str | None, all_langs: bool):
    q = "SELECT * FROM materials WHERE kind='reply' AND status='active' AND deleted_at IS NULL"
    args: list = []
    if lang and not all_langs:
        q += " AND lang=?"; args.append(lang)
    q += " ORDER BY usage_count ASC, id DESC"
    with get_conn() as conn:
        return conn.execute(q, args).fetchall()


async def pick_material_dialog(tweet_text: str, tweet_lang: str | None, title: str = "手动选素材"):
    """弹出素材选择框。返回 (material_id, final_text) 或 None（取消）。"""
    lang = tweet_lang or None
    with ui.dialog() as dlg, ui.card().classes("w-[760px] max-w-[95vw] max-h-[92vh] overflow-auto"):
        ui.label(title).classes("text-lg font-bold")
        with ui.card().classes("bg-slate-50 w-full"):
            ui.label("目标推文").classes("text-xs text-gray-500")
            ui.label(tweet_text).classes("text-sm whitespace-pre-wrap")
        with ui.row().classes("items-center gap-3"):
            all_sw = ui.switch(f"显示所有语言的素材（默认只显示与推文相同的「{LANG_NAME.get(lang or 'und', lang)}」）", value=not lang)
        state = {"mid": None}
        listbox = ui.column().classes("w-full gap-1")
        ui.label("选中一条后可在下面改文案，改完的内容会进审核队列（不会改素材库原文）。").classes("text-xs text-gray-400")
        ta = ui.textarea("最终文案", value="").classes("w-full").props("outlined autogrow")

        def render():
            listbox.clear()
            rows = _load_materials(lang, bool(all_sw.value))
            with listbox:
                if not rows:
                    ui.label("没有可用的回复素材（要求：类型=回复、状态=启用、不在回收站）。去「素材库」添加或用「AI 生成素材」。").classes("text-sm text-orange-600")
                    return
                for m in rows:
                    def choose(mm=m):
                        state["mid"] = mm["id"]; ta.value = mm["text"]
                        render()
                    sel = state["mid"] == m["id"]
                    with ui.card().classes("w-full cursor-pointer " + ("border-2 border-sky-500 bg-sky-50" if sel else "hover:bg-gray-50")).on("click", choose):
                        with ui.row().classes("items-center gap-2"):
                            ui.badge(m["lang"]).classes("bg-slate-500")
                            if m["scenario_tags"]:
                                ui.label("#" + m["scenario_tags"]).classes("text-xs text-gray-400")
                            ui.label(f"用过 {m['usage_count']} 次").classes("text-xs text-gray-400")
                            if m["created_by"] == "ai":
                                ui.badge("AI").classes("bg-purple-600")
                        ui.label(m["text"]).classes("text-sm whitespace-pre-wrap")
        all_sw.on("update:model-value", lambda e: render())
        render()

        def ok():
            if not state["mid"]:
                ui.notify("先点选一条素材", type="warning"); return
            if not (ta.value or "").strip():
                ui.notify("文案不能为空", type="negative"); return
            dlg.submit((state["mid"], ta.value.strip()))
        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("取消", on_click=lambda: dlg.submit(None)).props("flat")
            ui.button("生成待审核条目", icon="playlist_add", on_click=ok).props("color=primary")
    dlg.open()
    return await dlg


async def ai_write_dialog(jobs, target_id: int, tweet_text: str, default_brief: str = ""):
    """弹出「AI 撰写」框：填创作要求 → 调 LLM 生成 → 直接进待审核。返回 MatchOutcome 或 None。"""
    with ui.dialog() as dlg, ui.card().classes("w-[680px] max-w-[95vw]"):
        ui.label("AI 按要求撰写回复").classes("text-lg font-bold")
        with ui.card().classes("bg-slate-50 w-full"):
            ui.label("目标推文").classes("text-xs text-gray-500")
            ui.label(tweet_text).classes("text-sm whitespace-pre-wrap")
        brief = ui.textarea("创作要求", value=default_brief).classes("w-full").props("outlined autogrow")
        ui.label("写清楚：① 主题/立场（比如：推荐按量计费的多模型网关）；② 必须带的东西——直接把链接或 @账号写在要求里，"
                 "AI 会原样放进正文，少了会自动重写；③ 语气（比如：像同行随口聊，不像客服）。"
                 "AI 会先回应对方说的内容，再自然带出你的主题。").classes("text-xs text-gray-400")
        must_lbl = ui.label("").classes("text-xs text-sky-700")

        def upd():
            m = extract_must_include(brief.value or "")
            must_lbl.text = ("将强制包含：" + "、".join(m)) if m else "（没检测到链接或 @账号，正文里不会带任何链接/@）"
        brief.on("update:model-value", lambda e: upd()); upd()
        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("取消", on_click=lambda: dlg.submit(None)).props("flat")
            ui.button("生成并进待审核", icon="auto_awesome", on_click=lambda: dlg.submit(brief.value or "")).props("color=primary")
    dlg.open()
    text = await dlg
    if text is None:
        return None
    ui.notify("AI 撰写中…", type="info")
    try:
        outcome = await run.io_bound(jobs.match.ai_write, target_id, text)
    except Exception as e:
        notify_long(f"AI 撰写出错：{e}", ok=False, kind="negative"); return None
    notify_long(("已生成并进入待审核：" if outcome.status == "queued" else "没能生成：") + outcome.reason,
                ok=outcome.status == "queued")
    return outcome
