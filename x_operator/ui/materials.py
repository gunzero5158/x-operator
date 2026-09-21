"""素材库（design-v1.1 §8.3）：回复/发帖素材的增删改、启用/归档、回收站。

删除 = 软删除进回收站（deleted_at 打时间戳），回收站里可恢复或彻底删除。
进回收站的素材不会再被匹配引擎选中；引用它的定时发帖计划到点时会自动暂停。
"""
from __future__ import annotations

import sqlite3

from .i18n import Labels, t as _tr

from nicegui import run, ui

from ..core import langdetect, media
from ..core.langdetect import LANG_LABEL
from ..db.database import get_conn, utcnow_iso
from .layout import page_title, preview_text
from .layout import TAG, confirm, fmt_time, hint, llm_wait, shell
from .media_widget import MediaField, media_badge, media_strip

# 标签配色：一眼分清「干什么用的」（类型）、「现在能不能用」（状态）、「谁写的」
KIND_BADGE = {"reply": ("回复", TAG["reply"], "回复素材：用在别人的推文下面（自动匹配 / 换素材 时从这里挑）"),
              "post": ("发帖", TAG["post"], "发帖素材：自己账号发的主贴（定时发帖计划 从这里挑）")}
STATUS_BADGE = {"active": ("启用", TAG["ok"], "启用：会被匹配 / 定时发帖计划选中"),
                "draft": ("草稿", TAG["warn"], "草稿：还没启用，不会被选中"),
                "archived": ("归档", TAG["off"], "归档：保留但不再参与匹配")}

AUTO_LANG = "auto"  # 语言下拉里「自动判断」这一项的值，保存前会落成具体语言码


def resolve_lang(selected: str, text: str) -> str:
    """下拉选的是「自动判断」就按正文识别，否则用手选的；识别不出返回 ""。"""
    if selected and selected != AUTO_LANG:
        return selected
    return langdetect.detect(text)


def lang_hint_text(selected: str, text: str) -> str:
    """语言下拉下面那行小字：自动模式下实时告诉用户识别成了什么，方便发现误判后手选。"""
    if selected and selected != AUTO_LANG:
        return _tr('手选：{p0}。想改回自动请选「自动判断」。', p0=LANG_LABEL.get(selected, selected))
    if not (text or "").strip():
        return _tr('自动判断：输入正文后会按内容识别语言；识别不准可在上面手选。')
    code = langdetect.detect(text)
    if not code:
        return _tr('自动判断：暂时判不出语言（只有表情 / 链接 / 数字？），保存前请手选一个。')
    if code == langdetect.ZH_HANS and not langdetect.zh_script(text):
        return _tr('自动判断：识别为中文，但这段字简繁写法一样、看不出是哪种，先按「简体中文」存；要当繁体素材用请在上面手选「繁体中文」。')
    return _tr('自动判断：识别为「{p0}」，保存时按这个存；不对请在上面手选。', p0=LANG_LABEL.get(code, code))


def _load(kind_filter: str, status_filter: str, trash: bool):
    q = "SELECT * FROM materials WHERE deleted_at IS " + ("NOT NULL" if trash else "NULL")
    args: list = []
    if kind_filter != "all":
        q += " AND kind=?"; args.append(kind_filter)
    if status_filter != "all" and not trash:
        q += " AND status=?"; args.append(status_filter)
    q += " ORDER BY deleted_at DESC, COALESCE(translation_group_id, id), id" if trash \
        else " ORDER BY COALESCE(translation_group_id, id), id"
    with get_conn() as conn:
        return conn.execute(q, args).fetchall()


def _trash_count() -> int:
    with get_conn() as conn:
        return conn.execute("SELECT COUNT(*) AS c FROM materials WHERE deleted_at IS NOT NULL").fetchone()["c"]


def _save(mid, kind, text, lang, tags, status, files=None):
    mf = media.dump_files(files)
    with get_conn() as conn:
        if mid:
            conn.execute("UPDATE materials SET kind=?, text=?, lang=?, scenario_tags=?, status=?, media_files=? WHERE id=?",
                         (kind, text, lang, tags, status, mf, mid))
        else:
            conn.execute("INSERT INTO materials(kind, text, lang, scenario_tags, status, media_files, created_by) "
                         "VALUES (?,?,?,?,?,?,'human')", (kind, text, lang, tags, status, mf))
        conn.commit()


def _set_status(mid, status):
    with get_conn() as conn:
        conn.execute("UPDATE materials SET status=? WHERE id=?", (status, mid))
        conn.commit()


def _soft_delete(mid: int) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE materials SET deleted_at=? WHERE id=?", (utcnow_iso(), mid))
        conn.commit()


def _restore(mid: int) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE materials SET deleted_at=NULL WHERE id=?", (mid,))
        conn.commit()


def _hard_delete(mid: int) -> str:
    """彻底删除。被定时发帖计划引用则拒绝（返回原因）；任务队列里的引用置空后删除。"""
    with get_conn() as conn:
        n = conn.execute("SELECT COUNT(*) AS c FROM scheduled_posts WHERE material_id=?", (mid,)).fetchone()["c"]
        if n:
            return _tr('有 {p0} 个定时发帖计划引用这条素材，请先到「定时发帖计划」删除对应计划', p0=n)
        try:
            conn.execute("UPDATE review_queue SET material_id=NULL WHERE material_id=?", (mid,))
            conn.execute("UPDATE materials SET translation_group_id=NULL WHERE translation_group_id=?", (mid,))
            conn.execute("DELETE FROM materials WHERE id=?", (mid,))
            conn.commit()
        except sqlite3.IntegrityError as e:
            conn.rollback()
            return _tr('删除失败：{p0}', p0=e)
    return ""


def _empty_trash() -> tuple[int, int]:
    """清空回收站，返回 (删除数, 因被引用而保留数)。"""
    with get_conn() as conn:
        ids = [r["id"] for r in conn.execute("SELECT id FROM materials WHERE deleted_at IS NOT NULL").fetchall()]
    done = kept = 0
    for mid in ids:
        if _hard_delete(mid):
            kept += 1
        else:
            done += 1
    return done, kept


def register(jobs) -> None:
    @ui.page("/materials")
    def materials_page():
        with shell("/materials"):
            state = {"trash": False}
            with ui.row().classes("xo-page-heading w-full"):
                title = page_title(_tr('素材库'), _tr('整理可复用的文字、图片和视频'))
                with ui.row().classes("xo-toolbar w-full items-center gap-2"):
                    kind_f = ui.select({"all": _tr('全部类型'), "reply": _tr('回复'), "post": _tr('发帖')}, value="all").props("dense outlined")
                    status_f = ui.select({"all": _tr('全部状态'), "active": _tr('启用'), "draft": _tr('草稿'), "archived": _tr('归档')},
                                         value="all").props("dense outlined")
                    trash_btn = ui.button(_tr('回收站'), icon="delete_sweep", on_click=lambda: toggle_trash()).props("outline")
                    ai_btn = ui.button(_tr('AI 生成素材'), icon="auto_awesome", on_click=lambda: _ai_dialog(jobs, render)).props("outline color=purple")
                    new_btn = ui.button(_tr('新建素材'), icon="add", on_click=lambda: _edit_dialog(None, render)).props("color=primary")

            count_hint = ui.label("").classes("text-xs text-gray-400")
            with ui.expansion(_tr('素材用途与标签说明'), icon="info_outline").classes("xo-help w-full") as legend:
                with ui.row().classes("items-center gap-2 flex-wrap"):
                    ui.label(_tr('标签说明：')).classes("text-xs text-gray-500")
                    for _k, (_t, _c, _tip) in KIND_BADGE.items():
                        ui.badge(_t, color=None).classes(_c).tooltip(_tip)
                    ui.label(_tr('= 用途')).classes("text-xs text-gray-400 mr-2")
                    for _k, (_t, _c, _tip) in STATUS_BADGE.items():
                        ui.badge(_t, color=None).classes(_c).tooltip(_tip)
                    ui.label(_tr('= 状态')).classes("text-xs text-gray-400 mr-2")
                    ui.badge("AI", color=None).classes(TAG["ai"]); ui.label(_tr('= AI 生成')).classes("text-xs text-gray-400 mr-2")
                    ui.badge(_tr('📎 附件'), color=None).classes(TAG["media"]); ui.label(_tr('= 带配图/视频')).classes("text-xs text-gray-400")
            body = ui.column().classes("w-full gap-2")

            def toggle_trash():
                state["trash"] = not state["trash"]
                render()

            async def empty_trash():
                n = _trash_count()
                if not n:
                    ui.notify(_tr('回收站是空的'), type="info"); return
                if await confirm(_tr('清空回收站？'), _tr('将彻底删除 {p0} 条素材，无法恢复。', p0=n), ok_label=_tr('清空')):
                    done, kept = _empty_trash()
                    msg = _tr('已彻底删除 {p0} 条', p0=done)
                    if kept:
                        msg += _tr('，{p0} 条因被定时发帖计划引用而保留', p0=kept)
                    ui.notify(msg, type="positive")
                    render()

            async def hard_delete(mid: int):
                if await confirm(_tr('彻底删除这条素材？'), _tr('删除后无法恢复。'), ok_label=_tr('彻底删除')):
                    err = _hard_delete(mid)
                    ui.notify(err or _tr('已彻底删除'), type="negative" if err else "positive")
                    render()

            def render():
                body.clear()
                trash = state["trash"]
                tc = _trash_count()
                trash_btn.text = _tr('回收站（{p0}）', p0=tc) if not trash else _tr('返回素材列表')
                if trash:
                    trash_btn.props("color=negative")
                else:
                    trash_btn.props(remove="color=negative")
                title.text = _tr('素材库 · 回收站') if trash else _tr('素材库')
                status_f.set_visibility(not trash)
                legend.set_visibility(not trash)
                new_btn.set_visibility(not trash)
                ai_btn.set_visibility(not trash)
                count_hint.text = (_tr('回收站里的素材不会被匹配引擎使用；可恢复或彻底删除。') if trash
                             else _tr('「删除」会移入回收站（可恢复）；「归档」保留但不再参与匹配。'))
                rows = _load(kind_f.value, status_f.value, trash)
                with body:
                    if trash and tc:
                        with ui.row().classes("w-full justify-end"):
                            ui.button(_tr('清空回收站'), icon="delete_forever", on_click=empty_trash).props("color=negative outline dense")
                    if not rows:
                        ui.label(_tr('回收站是空的') if trash else _tr('暂无素材')).classes("text-gray-400")
                        return
                    for m in rows:
                        with ui.card().classes("xo-material-card w-full" + (" bg-red-50" if trash else "")):
                            files = media.parse_files(m["media_files"])
                            with ui.row().classes("items-center gap-2"):
                                kt, kc, ktip = KIND_BADGE.get(m["kind"], (m["kind"], "bg-slate-500", ""))
                                ui.badge(kt, color=None).classes(kc).tooltip(ktip)
                                ui.badge(_tr(langdetect.lang_name(m["lang"])), color=None).classes(TAG["metric"]).tooltip(_tr('语言'))
                                st, sc, stip = STATUS_BADGE.get(m["status"], (m["status"], "bg-gray-500", ""))
                                ui.badge(st, color=None).classes(sc).tooltip(stip)
                                if m["created_by"] == "ai":
                                    ui.badge("AI", color=None).classes(TAG["ai"]).tooltip(_tr('由「AI 生成素材」写的'))
                                media_badge(files)
                                if m["translation_group_id"]:
                                    ui.badge(_tr('翻译组 #{p0}', p0=m['translation_group_id']), color=None).classes("bg-teal-600")
                                ui.label(_tr('用 {p0} 次', p0=m['usage_count'])).classes("text-xs text-gray-400")
                                if m["scenario_tags"]:
                                    ui.label(_tr('场景：') + m["scenario_tags"].replace(",", ", ")).classes("text-xs text-gray-400").tooltip(_tr('场景标签只用于内部筛选（自动匹配 / 素材池），不会出现在推文里'))
                                if trash:
                                    ui.label(_tr('删除于 {p0}', p0=fmt_time(m['deleted_at']))).classes("text-xs text-red-400")
                            preview_text(m["text"])
                            media_strip(files)
                            with ui.row().classes("xo-actions gap-2"):
                                if trash:
                                    ui.button(_tr('恢复'), icon="restore", on_click=lambda mm=m: (_restore(mm["id"]), ui.notify(_tr('已恢复'), type="positive"), render())).props("flat")
                                    ui.button(_tr('彻底删除'), icon="delete_forever", on_click=lambda mm=m: hard_delete(mm["id"])).props("flat color=negative")
                                else:
                                    ui.button(_tr('编辑'), on_click=lambda mm=m: _edit_dialog(mm, render)).props("flat")
                                    if m["status"] == "active":
                                        ui.button(_tr('归档'), on_click=lambda mm=m: (_set_status(mm["id"], "archived"), render())).props("flat")
                                    else:
                                        ui.button(_tr('启用'), on_click=lambda mm=m: (_set_status(mm["id"], "active"), render())).props("flat")
                                    ui.button(_tr('删除'), icon="delete", on_click=lambda mm=m: (_soft_delete(mm["id"]), ui.notify(_tr('已移入回收站'), type="info"), render())).props("flat color=negative")

            kind_f.on("update:model-value", lambda e: render())
            status_f.on("update:model-value", lambda e: render())
            render()

    def _edit_dialog(m, refresh):
        with ui.dialog() as dialog, ui.card().classes("w-[640px] max-w-[95vw] max-h-[92vh] overflow-auto"):
            ui.label(_tr('编辑素材') if m else _tr('新建素材')).classes("text-lg font-bold")
            kind = ui.select({"reply": _tr('回复'), "post": _tr('发帖')}, value=m["kind"] if m else "reply", label=_tr('类型')).classes("w-full").props("outlined")
            lang_opts = {AUTO_LANG: _tr('自动判断（按正文内容）'), **LANG_LABEL}
            lang_init = (m["lang"] if m and m["lang"] in LANG_LABEL else AUTO_LANG)   # 旧数据的 zh（简繁未定）按自动判断重新识别
            lang = ui.select(lang_opts, value=lang_init, label=_tr('语言')).classes("w-full").props("outlined")
            text = ui.textarea(_tr('正文'), value=m["text"] if m else "").classes("w-full").props("outlined autogrow")
            lang_hint = ui.label().classes("text-xs text-gray-400 -mt-2 mb-1")

            def refresh_lang_hint():
                lang_hint.set_text(lang_hint_text(lang.value, text.value))

            text.on_value_change(lambda e: refresh_lang_hint())
            lang.on_value_change(lambda e: refresh_lang_hint())
            refresh_lang_hint()
            tags = ui.input(_tr('场景标签（逗号分隔）'), value=m["scenario_tags"] if m else "").classes("w-full").props("outlined")
            hint(_tr('只用于内部筛选：自动匹配时优先挑场景对得上的素材、定时发帖计划的素材池按标签选；不是推文里的 #话题，不会发出去。想带话题请直接写进正文。'))
            status = ui.select({"draft": _tr('草稿'), "active": _tr('启用'), "archived": _tr('归档')},
                               value=m["status"] if m else "active", label=_tr('状态')).classes("w-full").props("outlined")
            mf = MediaField(media.parse_files(m["media_files"]) if m else [],
                            note=_tr('这条素材被用来回复或发帖时，附件会一起发出去。'))

            def do_save():
                if not mf.ready():
                    return
                if not text.value.strip():
                    ui.notify(_tr('正文不能为空'), type="negative"); return
                final_lang = resolve_lang(lang.value, text.value)
                if not final_lang:
                    ui.notify(_tr('没能从正文判断出语言（只有表情 / 链接 / 数字？），请在「语言」里手选一个'), type="negative", multi_line=True); return
                err = media.check_set(mf.files)
                if err:
                    ui.notify(err, type="negative"); return
                _save(m["id"] if m else None, kind.value, text.value.strip(), final_lang,
                      tags.value.strip(), status.value, mf.files)
                dialog.close(); refresh(); ui.notify(_tr('已保存'), type="positive")

            with ui.row():
                ui.button(_tr('保存'), on_click=do_save).props("color=primary")
                ui.button(_tr('取消'), on_click=dialog.close).props("flat")
        dialog.open()

    async def _ai_dialog(jobs, refresh):
        """AI 批量生成素材：填主题/风格/语言/场景/必须包含 → 预览勾选 → 入库。"""
        if not jobs.llm.configured:
            ui.notify(_tr('「AI 生成素材」需要先到「设置 → LLM」配置网关'), type="warning", multi_line=True); return
        with ui.dialog() as dlg, ui.card().classes("w-[760px] max-w-[95vw] max-h-[92vh] overflow-auto"):
            ui.label(_tr('AI 生成素材')).classes("text-lg font-bold")
            with ui.row().classes("w-full gap-3 no-wrap"):
                kind = ui.select({"reply": _tr('回复（在别人推文下用）'), "post": _tr('发帖（自己发的推文）')}, value="reply", label=_tr('类型')).classes("flex-1").props("outlined")
                lang = ui.select(LANG_LABEL, value="ja", label=_tr('语言')).classes("flex-1").props("outlined")
                count = ui.number(_tr('生成条数'), value=5, min=1, max=20, step=1).classes("w-32").props("outlined")
            hint(_tr('类型决定口吻：回复=接着别人的话说；发帖=像账号主人日常发帖。条数推荐 5~10，一次太多会趋同。'))
            topic = ui.textarea(_tr('主题 / 要传达的信息'), value="").classes("w-full").props("outlined autogrow")
            ui.label(_tr('例：我们是面向独立开发者的 XX 工具，比同类产品便宜、上手快；主打省钱和省事。')).classes("text-xs text-gray-400 -mt-2 mb-1")
            style = ui.input(_tr('风格 / 语气'), value="").classes("w-full").props("outlined")
            ui.label(_tr('例：像同行随口聊天，不像客服；简短；可以带一点自嘲。留空=自然口语。')).classes("text-xs text-gray-400 -mt-2 mb-1")
            scenario = ui.input(_tr('使用场景（选填）'), value="").classes("w-full").props("outlined")
            hint(_tr('例：对方在抱怨某工具太贵 / 对方在问有没有替代方案。会写进素材的场景标签，方便匹配时优先选用。'))
            must = ui.input(_tr('必须包含（选填，多个用逗号）'), value="").classes("w-full").props("outlined")
            hint(_tr('例：@你的官号, https://你的官网 。会原样出现在每条里。提醒：在别人帖子下带外链容易被折叠/处罚，回复类建议只 @ 或不带。'))
            async def gen():
                if not (topic.value or "").strip():
                    ui.notify(_tr('请先填主题'), type="negative"); return
                if not gen_btn.enabled:
                    return
                gen_btn.disable()
                kind_value, lang_value = kind.value, lang.value
                args = (kind_value, lang_value, topic.value.strip(),
                        (style.value or "").strip(), (scenario.value or "").strip(),
                        [m.strip() for m in (must.value or "").replace("，", ",").split(",") if m.strip()],
                        int(count.value or 5))
                dlg.close()
                try:
                    async with llm_wait(_tr('AI 生成素材'), scene="material_gen",
                                        note=_tr('生成后可在任务面板查看预览，勾选确认后才会入库。')) as task:
                        items = await run.io_bound(jobs.llm.generate_materials, *args)
                        if items:
                            task.result_action = lambda: _material_preview(items, kind_value, lang_value, task)
                            task.finish(_tr('已生成 {p0} 条素材，点击「查看生成结果」选择并入库。', p0=len(items)))
                        else:
                            task.finish(_tr('AI 没有返回内容，请换个说法再试。'), ok=False)
                except Exception:
                    # 任务面板保留完整错误；离开原页面也能查看。
                    return

            with ui.row().classes("w-full justify-end gap-2"):
                ui.button(_tr('取消'), on_click=dlg.close).props("flat")
                gen_btn = ui.button(_tr('生成预览'), icon="auto_awesome", on_click=gen).props("color=purple")
        dlg.open()


def _material_preview(items: list[dict], kind: str, lang: str, task) -> None:
    """从任意页面打开生成结果；草稿数据不依赖最初的表单生命周期。"""
    if task.result_action is None:
        ui.notify(_tr('这批素材已入库'), type="info")
        return
    with ui.dialog() as dlg, ui.card().classes("w-[760px] max-w-[95vw] max-h-[92vh] overflow-auto"):
        ui.label(_tr('已生成 {p0} 条素材', p0=len(items))).classes("text-lg font-bold")
        ui.label(_tr('取消勾选不想要的内容，也可以直接修改正文。')).classes("text-sm text-slate-500")
        chosen, editors = {}, {}
        for i, item in enumerate(items):
            with ui.row().classes("w-full items-start gap-2 no-wrap"):
                chosen[i] = ui.checkbox(_tr('入库'), value=True)
                with ui.column().classes("flex-1 gap-0"):
                    editors[i] = ui.textarea(value=item["text"]).classes("w-full").props("outlined autogrow dense")
                    editors[i].on_value_change(lambda e, i=i: items[i].__setitem__("text", e.value))
                    ui.label(_tr('场景：') + (item.get("scenario_tags") or "")).classes("text-xs text-slate-500")
        status_sel = ui.select({"active": _tr('直接启用'), "draft": _tr('先存为草稿')}, value="active",
                               label=_tr('入库状态')).props("outlined dense")

        def save():
            if task.result_action is None:
                ui.notify(_tr('这批素材已入库'), type="info"); return
            picked = [i for i in chosen if chosen[i].value]
            if not picked:
                ui.notify(_tr('没有勾选任何一条'), type="warning"); return
            with get_conn() as conn:
                for i in picked:
                    conn.execute("INSERT INTO materials(kind, text, lang, scenario_tags, status, created_by) VALUES (?,?,?,?,?,'ai')",
                                 (kind, editors[i].value.strip(), lang, items[i].get("scenario_tags", ""), status_sel.value))
                conn.commit()
            task.result_action = None
            task.result_link = (_tr('查看素材库'), "/materials")
            task.finish(_tr('已入库 {p0} 条素材。', p0=len(picked)))
            dlg.close()
            ui.navigate.to("/materials")

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button(_tr('稍后处理'), on_click=dlg.close).props("flat")
            ui.button(_tr('入库勾选的'), icon="save", on_click=save).props("color=primary")
    dlg.open()


# Resolve display labels per client; keep core dictionaries and stored values unchanged.
LANG_LABEL = Labels(LANG_LABEL)
KIND_BADGE = Labels(KIND_BADGE)
STATUS_BADGE = Labels(STATUS_BADGE)
