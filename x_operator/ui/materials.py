"""素材库（design-v1.1 §8.3）：回复/发帖素材的增删改、启用/归档、回收站。

删除 = 软删除进回收站（deleted_at 打时间戳），回收站里可恢复或彻底删除。
进回收站的素材不会再被匹配引擎选中；引用它的定时计划到点时会自动暂停。
"""
from __future__ import annotations

import sqlite3

from nicegui import run, ui

from ..core import media
from ..db.database import get_conn, utcnow_iso
from .layout import confirm, fmt_time, shell
from .media_widget import MediaField, media_badge, media_strip

# 标签配色：一眼分清「干什么用的」（类型）、「现在能不能用」（状态）、「谁写的」
KIND_BADGE = {"reply": ("回复", "bg-indigo-600", "回复素材：用在别人的推文下面（自动匹配 / 换素材 时从这里挑）"),
              "post": ("发帖", "bg-orange-600", "发帖素材：自己账号发的主贴（定时计划 从这里挑）")}
STATUS_BADGE = {"active": ("启用", "bg-green-600", "启用：会被匹配 / 定时计划选中"),
                "draft": ("草稿", "bg-amber-500", "草稿：还没启用，不会被选中"),
                "archived": ("归档", "bg-gray-500", "归档：保留但不再参与匹配")}


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
    """彻底删除。被定时计划引用则拒绝（返回原因）；审核队列里的引用置空后删除。"""
    with get_conn() as conn:
        n = conn.execute("SELECT COUNT(*) AS c FROM scheduled_posts WHERE material_id=?", (mid,)).fetchone()["c"]
        if n:
            return f"有 {n} 个定时计划引用这条素材，请先到「定时计划」删除对应计划"
        try:
            conn.execute("UPDATE review_queue SET material_id=NULL WHERE material_id=?", (mid,))
            conn.execute("UPDATE materials SET translation_group_id=NULL WHERE translation_group_id=?", (mid,))
            conn.execute("DELETE FROM materials WHERE id=?", (mid,))
            conn.commit()
        except sqlite3.IntegrityError as e:
            conn.rollback()
            return f"删除失败：{e}"
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
            with ui.row().classes("items-center justify-between w-full"):
                title = ui.label("素材库").classes("text-2xl font-bold")
                with ui.row().classes("items-center gap-2"):
                    kind_f = ui.select({"all": "全部类型", "reply": "回复", "post": "发帖"}, value="all").props("dense outlined")
                    status_f = ui.select({"all": "全部状态", "active": "启用", "draft": "草稿", "archived": "归档"},
                                         value="all").props("dense outlined")
                    trash_btn = ui.button("回收站", icon="delete_sweep", on_click=lambda: toggle_trash()).props("outline")
                    ai_btn = ui.button("AI 生成素材", icon="auto_awesome", on_click=lambda: _ai_dialog(jobs, render)).props("outline color=purple")
                    new_btn = ui.button("新建素材", icon="add", on_click=lambda: _edit_dialog(None, render)).props("color=primary")

            hint = ui.label("").classes("text-xs text-gray-400")
            with ui.row().classes("items-center gap-2 flex-wrap") as legend:
                ui.label("标签说明：").classes("text-xs text-gray-500")
                for _k, (_t, _c, _tip) in KIND_BADGE.items():
                    ui.badge(_t).classes(_c).tooltip(_tip)
                ui.label("= 用途").classes("text-xs text-gray-400 mr-2")
                for _k, (_t, _c, _tip) in STATUS_BADGE.items():
                    ui.badge(_t).classes(_c).tooltip(_tip)
                ui.label("= 状态").classes("text-xs text-gray-400 mr-2")
                ui.badge("AI").classes("bg-purple-600"); ui.label("= AI 生成").classes("text-xs text-gray-400 mr-2")
                ui.badge("📎 附件").classes("bg-pink-600"); ui.label("= 带配图/视频").classes("text-xs text-gray-400")
            body = ui.column().classes("w-full gap-2")

            def toggle_trash():
                state["trash"] = not state["trash"]
                render()

            async def empty_trash():
                n = _trash_count()
                if not n:
                    ui.notify("回收站是空的", type="info"); return
                if await confirm(f"清空回收站？", f"将彻底删除 {n} 条素材，无法恢复。", ok_label="清空"):
                    done, kept = _empty_trash()
                    msg = f"已彻底删除 {done} 条"
                    if kept:
                        msg += f"，{kept} 条因被定时计划引用而保留"
                    ui.notify(msg, type="positive")
                    render()

            async def hard_delete(mid: int):
                if await confirm("彻底删除这条素材？", "删除后无法恢复。", ok_label="彻底删除"):
                    err = _hard_delete(mid)
                    ui.notify(err or "已彻底删除", type="negative" if err else "positive")
                    render()

            def render():
                body.clear()
                trash = state["trash"]
                tc = _trash_count()
                trash_btn.text = f"回收站（{tc}）" if not trash else "返回素材列表"
                if trash:
                    trash_btn.props("color=negative")
                else:
                    trash_btn.props(remove="color=negative")
                title.text = "素材库 · 回收站" if trash else "素材库"
                status_f.set_visibility(not trash)
                legend.set_visibility(not trash)
                new_btn.set_visibility(not trash)
                ai_btn.set_visibility(not trash)
                hint.text = ("回收站里的素材不会被匹配引擎使用；可恢复或彻底删除。" if trash
                             else "「删除」会移入回收站（可恢复）；「归档」保留但不再参与匹配。")
                rows = _load(kind_f.value, status_f.value, trash)
                with body:
                    if trash and tc:
                        with ui.row().classes("w-full justify-end"):
                            ui.button("清空回收站", icon="delete_forever", on_click=empty_trash).props("color=negative outline dense")
                    if not rows:
                        ui.label("回收站是空的" if trash else "暂无素材").classes("text-gray-400")
                        return
                    for m in rows:
                        with ui.card().classes("w-full" + (" bg-red-50" if trash else "")):
                            files = media.parse_files(m["media_files"])
                            with ui.row().classes("items-center gap-2"):
                                kt, kc, ktip = KIND_BADGE.get(m["kind"], (m["kind"], "bg-slate-500", ""))
                                ui.badge(kt).classes(kc).tooltip(ktip)
                                ui.badge(m["lang"]).classes("bg-slate-500").tooltip("语言")
                                st, sc, stip = STATUS_BADGE.get(m["status"], (m["status"], "bg-gray-500", ""))
                                ui.badge(st).classes(sc).tooltip(stip)
                                if m["created_by"] == "ai":
                                    ui.badge("AI").classes("bg-purple-600").tooltip("由「AI 生成素材」写的")
                                media_badge(files)
                                if m["translation_group_id"]:
                                    ui.badge(f"翻译组 #{m['translation_group_id']}").classes("bg-teal-600")
                                ui.label(f"用 {m['usage_count']} 次").classes("text-xs text-gray-400")
                                if m["scenario_tags"]:
                                    ui.label("#" + m["scenario_tags"]).classes("text-xs text-gray-400")
                                if trash:
                                    ui.label(f"删除于 {fmt_time(m['deleted_at'])}").classes("text-xs text-red-400")
                            ui.label(m["text"]).classes("text-sm whitespace-pre-wrap")
                            media_strip(files)
                            with ui.row().classes("gap-2"):
                                if trash:
                                    ui.button("恢复", icon="restore", on_click=lambda mm=m: (_restore(mm["id"]), ui.notify("已恢复", type="positive"), render())).props("flat")
                                    ui.button("彻底删除", icon="delete_forever", on_click=lambda mm=m: hard_delete(mm["id"])).props("flat color=negative")
                                else:
                                    ui.button("编辑", on_click=lambda mm=m: _edit_dialog(mm, render)).props("flat")
                                    if m["status"] == "active":
                                        ui.button("归档", on_click=lambda mm=m: (_set_status(mm["id"], "archived"), render())).props("flat")
                                    else:
                                        ui.button("启用", on_click=lambda mm=m: (_set_status(mm["id"], "active"), render())).props("flat")
                                    ui.button("删除", icon="delete", on_click=lambda mm=m: (_soft_delete(mm["id"]), ui.notify("已移入回收站", type="info"), render())).props("flat color=negative")

            kind_f.on("update:model-value", lambda e: render())
            status_f.on("update:model-value", lambda e: render())
            render()

    def _edit_dialog(m, refresh):
        with ui.dialog() as dialog, ui.card().classes("w-[640px] max-w-[95vw] max-h-[92vh] overflow-auto"):
            ui.label("编辑素材" if m else "新建素材").classes("text-lg font-bold")
            kind = ui.select({"reply": "回复", "post": "发帖"}, value=m["kind"] if m else "reply", label="类型").classes("w-full").props("outlined")
            lang = ui.select({"ja": "日语", "en": "英语", "zh": "中文"}, value=m["lang"] if m else "ja", label="语言").classes("w-full").props("outlined")
            text = ui.textarea("正文", value=m["text"] if m else "").classes("w-full").props("outlined autogrow")
            tags = ui.input("场景标签（逗号分隔）", value=m["scenario_tags"] if m else "").classes("w-full").props("outlined")
            status = ui.select({"draft": "草稿", "active": "启用", "archived": "归档"},
                               value=m["status"] if m else "active", label="状态").classes("w-full").props("outlined")
            mf = MediaField(media.parse_files(m["media_files"]) if m else [],
                            note="这条素材被用来回复或发帖时，附件会一起发出去。")

            def do_save():
                if not text.value.strip():
                    ui.notify("正文不能为空", type="negative"); return
                err = media.check_set(mf.files)
                if err:
                    ui.notify(err, type="negative"); return
                _save(m["id"] if m else None, kind.value, text.value.strip(), lang.value,
                      tags.value.strip(), status.value, mf.files)
                dialog.close(); refresh(); ui.notify("已保存", type="positive")

            with ui.row():
                ui.button("保存", on_click=do_save).props("color=primary")
                ui.button("取消", on_click=dialog.close).props("flat")
        dialog.open()

    async def _ai_dialog(jobs, refresh):
        """AI 批量生成素材：填主题/风格/语言/场景/必须包含 → 预览勾选 → 入库。"""
        if not jobs.llm.configured:
            ui.notify("「AI 生成素材」需要先到「设置 → LLM」配置网关", type="warning", multi_line=True); return
        with ui.dialog() as dlg, ui.card().classes("w-[760px] max-w-[95vw] max-h-[92vh] overflow-auto"):
            ui.label("AI 生成素材").classes("text-lg font-bold")
            with ui.row().classes("w-full gap-3 no-wrap"):
                kind = ui.select({"reply": "回复（在别人推文下用）", "post": "发帖（自己发的推文）"}, value="reply", label="类型").classes("flex-1").props("outlined")
                lang = ui.select({"ja": "日语", "en": "英语", "zh": "中文", "ko": "韩语"}, value="ja", label="语言").classes("flex-1").props("outlined")
                count = ui.number("生成条数", value=5, min=1, max=20, step=1).classes("w-32").props("outlined")
            ui.label("类型决定口吻：回复=接着别人的话说；发帖=像账号主人日常发帖。条数推荐 5~10，一次太多会趋同。").classes("text-xs text-gray-400 -mt-2 mb-1")
            topic = ui.textarea("主题 / 要传达的信息", value="").classes("w-full").props("outlined autogrow")
            ui.label("例：我们是面向独立开发者的 XX 工具，比同类产品便宜、上手快；主打省钱和省事。").classes("text-xs text-gray-400 -mt-2 mb-1")
            style = ui.input("风格 / 语气", value="").classes("w-full").props("outlined")
            ui.label("例：像同行随口聊天，不像客服；简短；可以带一点自嘲。留空=自然口语。").classes("text-xs text-gray-400 -mt-2 mb-1")
            scenario = ui.input("使用场景（选填）", value="").classes("w-full").props("outlined")
            ui.label("例：对方在抱怨某工具太贵 / 对方在问有没有替代方案。会写进素材的场景标签，方便匹配时优先选用。").classes("text-xs text-gray-400 -mt-2 mb-1")
            must = ui.input("必须包含（选填，多个用逗号）", value="").classes("w-full").props("outlined")
            ui.label("例：@你的官号, https://你的官网 。会原样出现在每条里。提醒：在别人帖子下带外链容易被折叠/处罚，回复类建议只 @ 或不带。").classes("text-xs text-gray-400 -mt-2 mb-1")
            preview = ui.column().classes("w-full gap-1")
            chosen: dict[int, bool] = {}
            items_holder: dict = {"items": []}
            status_sel = ui.select({"active": "直接启用", "draft": "先存为草稿"}, value="active", label="入库状态").classes("w-64").props("outlined dense")

            async def gen():
                if not (topic.value or "").strip():
                    ui.notify("请先填主题", type="negative"); return
                gen_btn.disable(); preview.clear()
                with preview:
                    ui.spinner(); ui.label("AI 生成中（10~40 秒）…").classes("text-xs text-gray-400")
                try:
                    items = await run.io_bound(jobs.llm.generate_materials, kind.value, lang.value, topic.value.strip(),
                                               (style.value or "").strip(), (scenario.value or "").strip(),
                                               [m.strip() for m in (must.value or "").replace("，", ",").split(",") if m.strip()],
                                               int(count.value or 5))
                except Exception as e:
                    preview.clear()
                    with preview:
                        ui.label(f"生成失败：{e}").classes("text-red-500 whitespace-pre-wrap")
                    gen_btn.enable(); return
                gen_btn.enable()
                items_holder["items"] = items
                chosen.clear()
                preview.clear()
                with preview:
                    if not items:
                        ui.label("AI 没有返回内容，换个说法再试").classes("text-orange-600"); return
                    ui.label(f"生成了 {len(items)} 条，取消勾选不想要的，可以直接在框里改：").classes("text-sm font-semibold")
                    for i, it in enumerate(items):
                        chosen[i] = True
                        with ui.row().classes("w-full items-start gap-2 no-wrap"):
                            cb = ui.checkbox(value=True)
                            cb.on("update:model-value", lambda e, i=i: chosen.__setitem__(i, bool(e.args)))
                            with ui.column().classes("flex-1 gap-0"):
                                ta = ui.textarea(value=it["text"]).classes("w-full").props("outlined autogrow dense")
                                ta.on("update:model-value", lambda e, i=i: items_holder["items"][i].__setitem__("text", e.args))
                                ui.label("#" + (it["scenario_tags"] or "")).classes("text-xs text-gray-400")

            def save_all():
                items = items_holder["items"]
                picked = [it for i, it in enumerate(items) if chosen.get(i)]
                if not picked:
                    ui.notify("没有勾选任何一条", type="warning"); return
                with get_conn() as conn:
                    for it in picked:
                        conn.execute("INSERT INTO materials(kind, text, lang, scenario_tags, status, created_by) VALUES (?,?,?,?,?,'ai')",
                                     (kind.value, (it["text"] or "").strip(), lang.value, it.get("scenario_tags", ""), status_sel.value))
                    conn.commit()
                dlg.close(); refresh(); ui.notify(f"已入库 {len(picked)} 条（标记为 AI 生成）", type="positive")

            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("取消", on_click=dlg.close).props("flat")
                gen_btn = ui.button("生成预览", icon="auto_awesome", on_click=gen).props("color=purple")
                ui.button("入库勾选的", icon="save", on_click=save_all).props("color=primary")
        dlg.open()
