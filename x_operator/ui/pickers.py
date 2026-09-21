"""各页共用的两个弹窗：手动选素材、AI 按要求撰写。抓取记录页与任务队列页都会用到。"""
from __future__ import annotations

import json

from .i18n import Labels, t as _tr

from nicegui import run, ui

from ..core import media
from ..core.langdetect import lang_name as source_lang_name, material_lang_tiers
from ..db.database import get_conn, utcnow_iso
from ..core.accounts import REPLY_ACCOUNT_MODE_LABEL, account_options, reply_account_setting
from ..core.matcher import REPLY_MODE_LABEL, extract_must_include
from .layout import confirm, llm_wait, notify_long
from .layout import hint as _hint
from .media_widget import MediaField, media_badge


# ====================================================================================
# 创作要求模板（存在 brief_templates 表里，跟其他数据一样在本机 data/x_operator.db）
# ====================================================================================
def load_templates() -> list:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM brief_templates ORDER BY usage_count DESC, updated_at DESC").fetchall()


def save_template(name: str, text: str) -> None:
    """同名覆盖。"""
    with get_conn() as conn:
        conn.execute("INSERT INTO brief_templates(name, text, created_at, updated_at) VALUES (?,?,?,?) "
                     "ON CONFLICT(name) DO UPDATE SET text=excluded.text, updated_at=excluded.updated_at",
                     (name, text, utcnow_iso(), utcnow_iso()))
        conn.commit()


def delete_template(tid: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM brief_templates WHERE id=?", (tid,))
        conn.commit()


def _bump_template(tid: int) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE brief_templates SET usage_count=usage_count+1 WHERE id=?", (tid,))
        conn.commit()


def template_controls(brief) -> None:
    """在创作要求文本框下面画一行：从模板选 / 存为模板 / 删除模板。brief 是那个 textarea。"""
    def options() -> dict:
        return {t["id"]: _tr('{p0}（用过 {p1} 次）', p0=t['name'], p1=t['usage_count']) for t in load_templates()}

    with ui.row().classes("w-full items-center gap-2 no-wrap"):
        sel = ui.select(options(), label=_tr('从模板选（填入上面的创作要求）'), with_input=False).classes("flex-1").props("outlined dense clearable")
        save_btn = ui.button(_tr('存为模板'), icon="bookmark_add").props("outline dense")
        del_btn = ui.button(icon="delete").props("flat dense color=negative").tooltip(_tr('删除当前选中的模板'))

    def on_pick(e):
        tid = sel.value
        if not tid:
            return
        row = next((t for t in load_templates() if t["id"] == tid), None)
        if row is None:
            return
        brief.value = row["text"]
        _bump_template(tid)
        ui.notify(_tr('已填入模板「{p0}」，可以在上面继续改', p0=row['name']), type="info")
    sel.on("update:model-value", on_pick)

    async def on_save():
        text = (brief.value or "").strip()
        if not text:
            ui.notify(_tr('上面的创作要求是空的，先写点内容再存'), type="warning"); return
        current = next((t for t in load_templates() if t["id"] == sel.value), None)
        with ui.dialog() as dlg, ui.card().classes("min-w-96"):
            ui.label(_tr('存为模板')).classes("text-lg font-bold")
            name_in = ui.input(_tr('模板名（同名会覆盖）'), value=current["name"] if current else text[:20]).classes("w-full").props("outlined dense")
            ui.label(_tr('起个一看就知道用在什么场景的名字，比如「日本独立开发者·抱怨太贵」')).classes("text-xs text-gray-400")
            with ui.row().classes("w-full justify-end gap-2"):
                ui.button(_tr('取消'), on_click=lambda: dlg.submit(None)).props("flat")
                ui.button(_tr('保存'), on_click=lambda: dlg.submit((name_in.value or "").strip())).props("color=primary")
        dlg.open()
        name = await dlg
        if not name:
            return
        save_template(name, text)
        sel.set_options(options())
        ui.notify(_tr('模板「{p0}」已保存，下次在这里直接选', p0=name), type="positive")
    save_btn.on_click(on_save)

    async def on_delete():
        current = next((t for t in load_templates() if t["id"] == sel.value), None)
        if current is None:
            ui.notify(_tr('先在下拉里选中一个模板'), type="warning"); return
        if await confirm(_tr('删除模板「{p0}」？', p0=current['name']), _tr('不影响已经填进规则/推主里的创作要求。')):
            delete_template(current["id"])
            sel.set_options(options(), value=None)
            ui.notify(_tr('已删除'), type="positive")
    del_btn.on_click(on_delete)


# 搜索规则 / 监控推主共用的「回复方式」三个字段及其说明
REPLY_HINTS = {
    "reply_mode": "抓到达标推文后怎么生成回复：匹配素材库=从你写好的回复素材里选（可控、省钱）；AI 按要求创作=每条现写（更贴合、需 LLM）；"
                  "只抓取=不自动生成，你在抓取记录里逐条手动选素材或让 AI 写。",
    "ai_brief": "给 AI 的创作要求：主题/立场、必须带的链接或 @账号（直接写在这里，会强制原样出现）、语气。例：我们做 XX 产品，"
                "回复要先接对方的话再提一句，像同行聊天，结尾带 @你的官号。",
    "polish": "开=允许 AI 在素材基础上轻微改写以衔接对方的话（不改核心信息和链接/@）；关=一字不改用素材原文。推荐关，除非素材是通用模板。",
    "auto_approve": "默认关：生成的回复进「待审核」，你看过批准才发。开了以后，AI 给这条回复的置信度 ≥ 下面的阈值就跳过人工审核、直接进「待发送」，"
                    "发送前的黑名单 / 冷却 / 时效 / 日上限检查照常。置信度：匹配素材 = AI 对「这条素材贴不贴这条推文」的信心；"
                    "AI 按要求创作 = AI 对「写出来的回复切题、可以直接发」的自评；低于阈值的仍进待审核。只对自动搜索 / 自动监控 / 「运行一次」生效，"
                    "手动选素材、手动 AI 撰写、「重新匹配」不受影响。开着意味着发出去之前没有人看过，建议先用高阈值试。",
    "media_mode": "AI 只写文字，附件按这里的设置原样带上：固定=下面放的几个每次都一起发（最多 4 个）；"
                  "素材池=下面放一批图片 / 视频（最多 30 个），每条回复随机挑 1 个，优先挑这条规则 / 推主最近没用过的。"
                  "匹配素材库模式的附件跟着素材走，在素材库里给素材加。",
    "reply_account": "这条规则/推主抓到的推文由哪个账号回复。自动轮流=在启用中的小号里挑最闲的（按今天已回+待发条数，跳过已到日上限的），"
                     "主号不参与；一个小号都没有时才退回主号。只用指定的账号=选 1 个就固定用它，选多个就只在这几个里按同样的规则轮流"
                     "（主号选进来也参与）。排除某些账号=照常自动轮流，但名单里的号不参与，排除完一个都不剩时不会生成草稿。"
                     "任务队列里每条也能临时改。推荐自动轮流。",
}


hint = _hint   # 输入框下面的说明（共用实现在 layout.hint：长说明折成一行，点「更多」展开）


MEDIA_MODE_LABEL = {"fixed": "固定：每条回复都带下面这几个", "pool": "素材池：每条回复从下面随机挑 1 个"}
MEDIA_FIELD_LABEL = {"fixed": "随 AI 写的回复一起发的配图 / 视频（选填）", "pool": "配图 / 视频素材池（选填，每条随机挑 1 个）"}


class ReplyAccountField:
    """「回复账号」：设法下拉（自动轮流 / 只用指定的 / 排除某些）+ 账号多选名单（自动轮流时隐藏）。"""
    LIST_LABEL = {"include": "用哪些账号回复（选 1 个 = 固定，多个 = 轮流）", "exclude": "不参与轮流的账号"}

    def __init__(self, cfg=None):
        mode_value, ids_value = reply_account_setting(cfg)
        opts = account_options(with_auto=False)
        self.mode = ui.select(REPLY_ACCOUNT_MODE_LABEL, value=mode_value, label=_tr('回复账号')).classes("w-full").props("outlined")
        self.ids = ui.select(opts, value=[i for i in ids_value if i in opts], multiple=True,
                             label=self.LIST_LABEL.get(mode_value, _tr('账号'))).classes("w-full").props("outlined use-chips")
        self.mode.on("update:model-value", lambda e: self._sync()); self._sync()

    def _sync(self):
        listed = self.mode.value in self.LIST_LABEL
        self.ids.set_visibility(listed)
        if listed:
            self.ids._props["label"] = self.LIST_LABEL[self.mode.value]
            self.ids.update()

    def values(self) -> tuple[str, str]:
        """保存时取 (reply_account_mode, reply_account_ids JSON)。自动轮流时名单清空。"""
        mode = self.mode.value if self.mode.value in REPLY_ACCOUNT_MODE_LABEL else "auto"
        ids = [int(i) for i in (self.ids.value or []) if i] if mode != "auto" else []
        return mode, json.dumps(ids)

    def invalid(self) -> str:
        if self.mode.value == "include" and not self.ids.value:
            return _tr('回复账号选了「只用指定的账号」，但名单里一个账号都没选')
        if self.mode.value == "exclude" and not self.ids.value:
            return _tr('回复账号选了「排除某些账号」，但没选要排除哪些（不排除就选「自动轮流」）')
        return ""


def reply_mode_fields(mode_value: str, brief_value: str, polish_value: bool, mode_label: str,
                      account_cfg=None, auto_approve_value: bool = False,
                      auto_threshold_value: float = 0.7, media_files_value: str = "[]", media_mode_value: str = "fixed"):
    """画出「回复方式 / AI 创作要求 / AI 创作附件 / 允许润色 / 回复账号 / 免审核」控件（带说明、按模式显隐），
    account_cfg：规则 / 推主那一行（新建时 None），用来读回复账号的设法。
    返回 (mode, brief, polish, acc, auto_sw, auto_thr, media_mode, mf)，acc 是 ReplyAccountField。"""
    ui.separator()
    ui.label(_tr('回复方式')).classes("font-semibold text-sm")
    mode = ui.select(REPLY_MODE_LABEL, value=mode_value if mode_value in REPLY_MODE_LABEL else "material",
                     label=mode_label).classes("w-full").props("outlined")
    hint(REPLY_HINTS["reply_mode"])
    acc = ReplyAccountField(account_cfg)
    hint(REPLY_HINTS["reply_account"])
    brief = ui.textarea(_tr('AI 创作要求'), value=brief_value or "").classes("w-full").props("outlined autogrow")
    brief_hint = hint(REPLY_HINTS["ai_brief"])
    tpl_box = ui.column().classes("w-full gap-0")
    with tpl_box:
        template_controls(brief)
    media_box = ui.column().classes("w-full gap-4")
    with media_box:
        media_mode = ui.select(MEDIA_MODE_LABEL, value=media_mode_value if media_mode_value in MEDIA_MODE_LABEL else "fixed",
                               label=_tr('配图 / 视频怎么带')).classes("w-full").props("outlined")
        hint(REPLY_HINTS["media_mode"], after_row=True)
        mf = MediaField(media.parse_files(media_files_value or "[]"), label=MEDIA_FIELD_LABEL["fixed"])

        def sync_media_mode():
            pool = media_mode.value == "pool"
            mf.set_limit(media.POOL_MAX_ITEMS if pool else media.MAX_ITEMS, "", label=MEDIA_FIELD_LABEL[media_mode.value])
        media_mode.on("update:model-value", lambda e: sync_media_mode()); sync_media_mode()
    polish = ui.switch(_tr('允许 AI 轻微润色素材'), value=bool(polish_value))
    polish_hint = hint(REPLY_HINTS["polish"])

    auto_box = ui.column().classes("w-full gap-4")
    with auto_box:
        with ui.row().classes("w-full items-center gap-3 no-wrap"):
            auto_sw = ui.switch(_tr('免审核：置信度达标直接进待发送'), value=bool(auto_approve_value))
            auto_thr = ui.number(_tr('置信度阈值（0~1）'), value=auto_threshold_value if auto_threshold_value is not None else 0.7,
                                 min=0, max=1, step=0.05).props("outlined dense").classes("w-44")
        hint(REPLY_HINTS["auto_approve"], after_row=True)

    def warn_auto(e):
        if e.args:
            ui.notify(_tr('已打开免审核：这条规则 / 推主生成的回复只要置信度达到阈值，就会不经人看直接进待发送'), type="warning", multi_line=True, timeout=8000)
    auto_sw.on("update:model-value", warn_auto)

    def sync():
        is_ai = mode.value == "ai_write"
        brief.set_visibility(is_ai); brief_hint.set_visibility(is_ai); tpl_box.set_visibility(is_ai); media_box.set_visibility(is_ai)
        polish.set_visibility(mode.value == "material"); polish_hint.set_visibility(mode.value == "material")
        auto_box.set_visibility(mode.value != "manual")     # 只抓取模式没有自动生成的回复，免审核无意义
        auto_thr.set_visibility(bool(auto_sw.value))
    mode.on("update:model-value", lambda e: sync()); auto_sw.on("update:model-value", lambda e: sync()); sync()
    return mode, brief, polish, acc, auto_sw, auto_thr, media_mode, mf


def media_values(media_mode, mf) -> tuple[str, str]:
    """保存时取 (media_mode, media_files JSON)。"""
    return (media_mode.value if media_mode.value in MEDIA_MODE_LABEL else "fixed"), media.dump_files(mf.files)


def auto_approve_values(auto_sw, auto_thr) -> tuple[int, float]:
    """保存时取免审核开关与阈值（阈值钳到 0~1，填错按 0.7）。"""
    try:
        thr = min(max(float(auto_thr.value), 0.0), 1.0)
    except (TypeError, ValueError):
        thr = 0.7
    return (1 if auto_sw.value else 0), round(thr, 2)


def reply_mode_invalid(mode, brief, media_mode=None, mf=None, acc: ReplyAccountField | None = None) -> str:
    """保存前校验，返回中文错误（空串 = 没问题）。"""
    if mf is not None and mf.is_uploading:
        return _tr('附件还在上传或保存，请等上传完成后再保存')
    if acc is not None and acc.invalid():
        return acc.invalid()
    if mode.value == "ai_write" and not (brief.value or "").strip():
        return _tr('选了「AI 按要求创作」就必须填创作要求')
    if mode.value == "ai_write" and mf is not None and mf.files:
        pool = media_mode is not None and media_mode.value == "pool"
        problem = media.check_set(mf.files, media.POOL_MAX_ITEMS if pool else media.MAX_ITEMS)
        if problem:
            return problem + ("" if pool else _tr('（想放更多请把「配图 / 视频怎么带」改成素材池）'))
    return ""


def _load_materials(lang: str | None, all_langs: bool):
    q = "SELECT * FROM materials WHERE kind='reply' AND status='active' AND deleted_at IS NULL"
    args: list = []
    if lang and not all_langs:
        codes = material_lang_tiers(lang)[0]   # 繁体推文也列出旧的「中文（简繁未定）」素材；简繁未定的推文简繁都列
        q += f" AND lang IN ({','.join('?' * len(codes))})"; args.extend(codes)
    q += " ORDER BY usage_count ASC, id DESC"
    with get_conn() as conn:
        return conn.execute(q, args).fetchall()


async def pick_material_dialog(tweet_text: str, tweet_lang: str | None, title: str = _tr('手动选素材')):
    """弹出素材选择框。返回 (material_id, final_text) 或 None（取消）。"""
    lang = tweet_lang or None
    with ui.dialog() as dlg, ui.card().classes("w-[760px] max-w-[95vw] max-h-[92vh] overflow-auto"):
        ui.label(title).classes("text-lg font-bold")
        with ui.card().classes("bg-slate-50 w-full"):
            ui.label(_tr('目标推文')).classes("text-xs text-gray-500")
            ui.label(tweet_text).classes("text-sm whitespace-pre-wrap")
        with ui.row().classes("items-center gap-3"):
            all_sw = ui.switch(_tr('显示所有语言的素材（默认只显示与推文相同的「{p0}」）', p0=lang_name(lang or 'und')), value=not lang)
        state = {"mid": None}
        listbox = ui.column().classes("w-full gap-1")
        ui.label(_tr('选中一条后可在下面改文案，改完的内容会进任务队列（不会改素材库原文）。')).classes("text-xs text-gray-400")
        ta = ui.textarea(_tr('最终文案'), value="").classes("w-full").props("outlined autogrow")

        def render():
            listbox.clear()
            rows = _load_materials(lang, bool(all_sw.value))
            with listbox:
                if not rows:
                    ui.label(_tr('没有可用的回复素材（要求：类型=回复、状态=启用、不在回收站）。去「素材库」添加或用「AI 生成素材」。')).classes("text-sm text-orange-600")
                    return
                for m in rows:
                    def choose(mm=m):
                        state["mid"] = mm["id"]; ta.value = mm["text"]
                        render()
                    sel = state["mid"] == m["id"]
                    with ui.card().classes("w-full cursor-pointer " + ("border-2 border-sky-500 bg-sky-50" if sel else "hover:bg-gray-50")).on("click", choose):
                        with ui.row().classes("items-center gap-2"):
                            ui.badge(lang_name(m["lang"]), color=None).classes("bg-slate-500")
                            if m["scenario_tags"]:
                                hint(_tr('场景：') + m["scenario_tags"].replace(",", ", "), after_row=True).tooltip(_tr('场景标签只用于内部筛选（自动匹配 / 素材池），不会出现在推文里'))
                            ui.label(_tr('用过 {p0} 次', p0=m['usage_count'])).classes("text-xs text-gray-400")
                            if m["created_by"] == "ai":
                                ui.badge("AI", color=None).classes("bg-purple-600")
                            media_badge(media.parse_files(m["media_files"]))
                        ui.label(m["text"]).classes("text-sm whitespace-pre-wrap")
        all_sw.on("update:model-value", lambda e: render())
        render()

        def ok():
            if not state["mid"]:
                ui.notify(_tr('先点选一条素材'), type="warning"); return
            if not (ta.value or "").strip():
                ui.notify(_tr('文案不能为空'), type="negative"); return
            dlg.submit((state["mid"], ta.value.strip()))
        with ui.row().classes("w-full justify-end gap-2"):
            ui.button(_tr('取消'), on_click=lambda: dlg.submit(None)).props("flat")
            ui.button(_tr('生成待审核条目'), icon="playlist_add", on_click=ok).props("color=primary")
    dlg.open()
    return await dlg


async def ai_write_dialog(jobs, target_id: int, tweet_text: str, default_brief: str = ""):
    """弹出「AI 撰写」框：填创作要求 → 调 LLM 生成 → 直接进待审核。返回 MatchOutcome 或 None。"""
    client = ui.context.client
    with ui.dialog() as dlg, ui.card().classes("w-[680px] max-w-[95vw] max-h-[92vh] overflow-auto"):
        ui.label(_tr('AI 按要求撰写回复')).classes("text-lg font-bold")
        with ui.card().classes("bg-slate-50 w-full"):
            ui.label(_tr('目标推文')).classes("text-xs text-gray-500")
            ui.label(tweet_text).classes("text-sm whitespace-pre-wrap")
        brief = ui.textarea(_tr('创作要求'), value=default_brief).classes("w-full").props("outlined autogrow")
        hint(_tr('写清楚：① 主题/立场（比如：推荐我们的 XX 产品）；② 必须带的东西——直接把链接或 @账号写在要求里，AI 会原样放进正文，少了会自动重写；③ 语气（比如：像同行随口聊，不像客服）。AI 会先回应对方说的内容，再自然带出你的主题。'), after_row=True)
        template_controls(brief)
        must_lbl = ui.label("").classes("text-xs text-sky-700")

        def upd():
            m = extract_must_include(brief.value or "")
            must_lbl.text = (_tr('将强制包含：') + "、".join(m)) if m else _tr('（没检测到链接或 @账号，正文里不会带任何链接/@）')
        brief.on("update:model-value", lambda e: upd()); upd()
        mf = MediaField([], label=_tr('随这条回复一起发的配图 / 视频（选填）'), note=_tr('AI 只写文字，附件原样带上。'))
        def generate():
            if mf.ready():
                dlg.submit((brief.value or "", list(mf.files)))
        with ui.row().classes("w-full justify-end gap-2"):
            ui.button(_tr('取消'), on_click=lambda: dlg.submit(None)).props("flat")
            ui.button(_tr('生成并进待审核'), icon="auto_awesome", on_click=generate).props("color=primary")
    dlg.open()
    res = await dlg
    if res is None:
        return None
    text, files = res
    try:
        async with llm_wait(_tr('AI 撰写回复'), scene="write", result_link=(_tr('查看待审核'), "/queue?status=pending")) as task:
            outcome = await run.io_bound(jobs.match.ai_write, target_id, text, None, "ai_write", "", files)
            task.finish((_tr('已进入待审核：') if outcome.status == "queued" else _tr('没能生成：')) + outcome.reason,
                        ok=outcome.status == "queued")
    except Exception as e:
        if not client.is_deleted:
            with client.content:
                notify_long(_tr('AI 撰写出错：{p0}', p0=e), ok=False, kind="negative")
        return None
    if not client.is_deleted:
        with client.content:
            notify_long((_tr('已生成并进入待审核：') if outcome.status == "queued" else _tr('没能生成：')) + outcome.reason,
                        ok=outcome.status == "queued")
    return outcome


# Resolve display labels per client; keep core dictionaries and stored values unchanged.
REPLY_ACCOUNT_MODE_LABEL = Labels(REPLY_ACCOUNT_MODE_LABEL)
REPLY_MODE_LABEL = Labels(REPLY_MODE_LABEL)
REPLY_HINTS = Labels(REPLY_HINTS)
MEDIA_MODE_LABEL = Labels(MEDIA_MODE_LABEL)
MEDIA_FIELD_LABEL = Labels(MEDIA_FIELD_LABEL)
ReplyAccountField.LIST_LABEL = Labels(ReplyAccountField.LIST_LABEL)


def lang_name(code):
    return _tr(source_lang_name(code))
