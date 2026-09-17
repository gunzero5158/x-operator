"""MatchEngine（design-v1.1 §7.3）：为一条命中推文生成回复草稿，写入任务队列。

三条路线（由来源规则/推主的 reply_mode 决定，抓取记录页也可对单条手动触发）：
- material ：从素材库挑同语言、启用中的「回复」素材。有 LLM 时由 LLM 择优，否则启发式；
             默认原文使用素材（allow_polish=1 时允许 LLM 轻微润色，但不得改核心信息/链接/@）。
- ai_write ：不用素材库，按 ai_brief（主题、立场、必须带的链接/@、语气）让 LLM 现写。需要 LLM。
- manual   ：不自动生成，留在抓取记录里等人手动「选素材」或「AI 撰写」。
手动路线：manual_match(target_id, material_id, text) / ai_write(target_id, brief) 直接进队列。
LLM 异常按 no_match 处理（不阻断流水线），原因写进 target 供抓取记录页展示。
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from threading import Lock
from datetime import datetime, timedelta, timezone
from typing import Literal

from .. import config
from ..db.database import get_conn, to_iso, utcnow_iso
from ..llm.client import LLMClient, LLMError
from . import media, textlimit
from .accounts import choose_reply_account, fallback_reply_account
from .langdetect import lang_name, material_lang_tiers

REPLY_MODE_LABEL = {"material": "匹配素材库", "ai_write": "AI 按要求创作", "manual": "只抓取，手动处理"}


@dataclass(frozen=True)
class MatchOutcome:
    status: Literal["queued", "no_match", "skipped"]
    queue_id: int | None
    reason: str


@dataclass
class BatchMatchResult:
    total: int
    queued: int = 0
    no_match: int = 0
    skipped: int = 0
    failed: int = 0
    details: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (self.failed or self.no_match)

    def as_msg(self) -> str:
        summary = (f"共 {self.total} 条：进入待审核 {self.queued} 条，未匹配 {self.no_match} 条，"
                   f"跳过 {self.skipped} 条，出错 {self.failed} 条。")
        return summary + ("\n" + "\n".join(self.details) if self.details else "")


def auto_approve_threshold(cfg) -> float | None:
    """这条规则 / 这个推主的「免审核」：关着返回 None；开着返回置信度阈值（0~1）。"""
    if not _cfg_get(cfg, "auto_approve", 0):
        return None
    try:
        return min(max(float(_cfg_get(cfg, "auto_approve_min_confidence", 0.7)), 0.0), 1.0)
    except (TypeError, ValueError):
        return 0.7


def _cfg_get(cfg, key: str, default):
    if cfg is None:
        return default
    try:
        v = cfg[key]
    except (IndexError, KeyError):
        return default
    return default if v is None else v


def extract_must_include(brief: str) -> list[str]:
    """从创作要求里抽出「必须原样出现」的东西：链接和 @账号。"""
    items = re.findall(r"https?://\S+", brief or "")
    items += re.findall(r"@[A-Za-z0-9_]{1,15}", brief or "")
    seen, out = set(), []
    for x in items:
        x = x.rstrip("，。,.、)）]」』")
        if x and x not in seen:
            seen.add(x); out.append(x)
    return out


def load_source_cfg(target: sqlite3.Row) -> sqlite3.Row | None:
    """抓取记录 → 它来自哪条规则/哪个推主（含 reply_mode / ai_brief / allow_polish）。"""
    if not target["source_rule_id"]:
        return None
    table = "search_rules" if target["source"] == "search" else "watched_users"
    with get_conn() as conn:
        return conn.execute(f"SELECT * FROM {table} WHERE id=?", (target["source_rule_id"],)).fetchone()


class MatchEngine:
    def __init__(self, llm: LLMClient):
        self.llm = llm
        self._rematch_lock = Lock()
        self._rematching: set[int] = set()

    # ---------------- 素材候选 ----------------
    def pick_candidates(self, lang: str, tags: list[str], limit: int = 15) -> tuple[list[sqlite3.Row], str]:
        """返回 (候选列表, 语言说明)。优先同语言的启用回复素材；繁体推文没有繁体素材时先退到简体（反之亦然），
        再没有就退回全部语言（宁可给一条让人审，也不空手）。语言说明为空 = 用的是同语言素材。"""
        base = "SELECT * FROM materials WHERE kind='reply' AND status='active' AND deleted_at IS NULL"
        order = " ORDER BY usage_count ASC, COALESCE(last_used_at,'') ASC"
        same, sibling = material_lang_tiers(lang)
        note = ""
        with get_conn() as conn:
            def by_langs(codes: list[str]) -> list[sqlite3.Row]:
                if not codes:
                    return []
                return conn.execute(base + f" AND lang IN ({','.join('?' * len(codes))})" + order, codes).fetchall()
            rows = by_langs(same)
            if not rows:
                rows = by_langs(sibling)
                if rows:
                    note = (f"（素材库没有{lang_name(lang)}的回复素材，这次用的是{lang_name(sibling[0])}素材，审核时注意简繁"
                            "——规则里打开「允许 AI 轻微润色」可以让 AI 顺手转成推文的字形）")
            if not rows:
                rows = conn.execute(base + order).fetchall()
                note = f"（素材库没有「{lang_name(lang)}」的回复素材，这次从全部语言里挑的，审核时注意语言）"
        if not rows:
            return [], note
        tagset = set(t for t in tags if t)

        def overlap(m: sqlite3.Row) -> int:
            mtags = set(x for x in (m["scenario_tags"] or "").split(",") if x)
            return 1 if (tagset & mtags) else 0

        rows_sorted = sorted(rows, key=lambda m: (-overlap(m),))
        return rows_sorted[:limit], note

    # ---------------- 自动路线 ----------------
    def run(self, target: sqlite3.Row, account: sqlite3.Row, cfg: sqlite3.Row | None = None,
            pipeline: bool = True) -> MatchOutcome:
        """account = 抓取用的账号；真正用哪个账号回复由规则/推主的「回复账号」决定（见 core/accounts.py）。
        pipeline：搜索 / 监控自动调用为 True（才按这条规则 / 推主的「免审核」设置决定是否直接进待发送）；界面上人点的「重新匹配」传 False。"""
        if cfg is None:
            cfg = load_source_cfg(target)
        mode = _cfg_get(cfg, "reply_mode", "material")
        if mode == "manual":
            self._mark_no_match(target["id"], "规则设置为「只抓取，手动处理」：请在这里点「选素材」或「AI 撰写」")
            return MatchOutcome("no_match", None, "等待手动处理")
        reply_acc, acc_note = choose_reply_account(cfg, account)
        if reply_acc is None:
            self._mark_no_match(target["id"], acc_note)
            return MatchOutcome("no_match", None, acc_note)
        auto_thr = auto_approve_threshold(cfg) if pipeline else None
        if mode == "ai_write":
            brief = (_cfg_get(cfg, "ai_brief", "") or "").strip()
            if not brief:
                self._mark_no_match(target["id"], "规则选了「AI 按要求创作」但没填创作要求，请编辑规则补上")
                return MatchOutcome("no_match", None, "缺少创作要求")
            files, media_note = self._pipeline_media(cfg, target)
            if media_note:
                acc_note = (acc_note + "｜" if acc_note else "") + media_note
            return self.ai_write(target["id"], brief, account=reply_acc, origin="ai_write", acc_note=acc_note, auto_threshold=auto_thr,
                                 media_files=files)
        return self._match_material(target, reply_acc, bool(_cfg_get(cfg, "allow_polish", 0)), acc_note=acc_note, auto_threshold=auto_thr)

    @staticmethod
    def _pipeline_media(cfg, target: sqlite3.Row) -> tuple[list[str], str]:
        """规则 / 推主上挂的 AI 创作附件：固定 = 全带；素材池 = 随机挑 1 个，优先挑这条规则最近没用过的。返回 (附件列表, 说明)。"""
        files = media.parse_files(_cfg_get(cfg, "media_files", "[]") or "[]")
        if not files:
            return [], ""
        if (_cfg_get(cfg, "media_mode", "fixed") or "fixed") != "pool":
            return files, ""
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT rq.final_media_files FROM review_queue rq JOIN target_tweets tt ON tt.id=rq.target_tweet_id "
                "WHERE tt.source=? AND tt.source_rule_id=? AND rq.status<>'failed' ORDER BY rq.id DESC LIMIT ?",
                (target["source"], target["source_rule_id"], max(len(files), 1))).fetchall()
        recent: list[str] = []
        for r in rows:
            for f in media.parse_files(r["final_media_files"]):
                if f not in recent:
                    recent.append(f)
        picked = media.pick_from_pool(files, recent)
        return picked, f"配图从素材池（{len(files)} 个）里随机挑了 1 个"

    def _match_material(self, target: sqlite3.Row, account: sqlite3.Row, allow_polish: bool,
                        acc_note: str = "", auto_threshold: float | None = None,
                        keep_original: bool = False) -> MatchOutcome:
        """「宽进」：只要素材库里有启用的回复素材，就一定给出一条草稿进待审核——AI 择优；AI 拒绝/出错/说跳过/信心太低时
        退回到规则挑选（同语言里用得最少的一条），理由里写明，让审核的人知道这条是兜底出来的。"""
        lang = target["lang"] or "ja"
        if keep_original:
            allow_polish = False
        tags = _infer_tags(target["text"])
        candidates, lang_note = self.pick_candidates(lang, tags)
        if keep_original:
            lang_note = lang_note.replace("——规则里打开「允许 AI 轻微润色」可以让 AI 顺手转成推文的字形", "")
        if not candidates:
            reason = "素材库里没有任何状态为「启用」的回复素材。到素材库添加或启用回复素材后再点「素材库匹配」，也可在这里「AI 撰写」"
            self._mark_no_match(target["id"], reason)
            return MatchOutcome("no_match", None, reason)

        cand_payload = [{"material_id": c["id"], "text": c["text"], "lang": c["lang"]} for c in candidates]
        decision: dict = {}
        fallback_why = ""
        try:
            decision = self.llm.match_reply(target["text"], lang, cand_payload, allow_polish=allow_polish) or {}
        except LLMError as e:
            fallback_why = f"AI 匹配出错（{str(e)[:120]}）"
        threshold = config.get_float("match_confidence_threshold", 0.4)
        confidence = _to_float(decision.get("confidence"), 0.0)
        if not fallback_why and (decision.get("skip") or confidence < threshold):
            fallback_why = "AI 认为都不太贴（" + (str(decision.get("reason") or f"信心 {confidence:.2f} 低于 {threshold:.2f}")) + "）"

        if fallback_why:
            # 兜底：同语言里用得最少的那条（candidates 已按用量升序）
            chosen = candidates[0]
            reply_text = chosen["text"]
            confidence = 0.35
            reason = f"{fallback_why}，先按规则给你一条用得最少的素材，请审核时把关{lang_note}"
        else:
            try:
                material_id = int(decision.get("material_id"))   # LLM 可能返回字符串 "12"
            except (TypeError, ValueError):
                material_id = None
            chosen = next((c for c in candidates if c["id"] == material_id), None) or candidates[0]
            # 不允许润色时，一律用素材原文（防 LLM 自由发挥）
            reply_text = (decision.get("reply_text") or "").strip() if allow_polish else chosen["text"]
            if not reply_text:
                reply_text = chosen["text"]
            reason = ("AI 择优" if self.llm.configured else "启发式") + (
                "（已按规则允许轻微润色）" if allow_polish else "（素材原文）") + "：" + str(decision.get("reason") or "") + lang_note
        if acc_note:
            reason += f"｜{acc_note}"
        if keep_original:
            # 显式选择素材库时只取已有文案，不因原规则或长度限制再次触发生成。
            reply_text = chosen["text"]
            reason = "素材库匹配（保留素材原文和附件）｜" + reason
            len_note = (textlimit.over_message(reply_text, account) + "，请在待审核中手动删减或更换素材"
                        if textlimit.over_by(reply_text, account) else "")
        else:
            reply_text, len_note = textlimit.fit(reply_text, account, self.llm, extract_must_include(reply_text), lang)
        if len_note:
            reason += f"｜{len_note}"
        qid = self._enqueue(account["id"], target["id"], chosen["id"], reply_text, reason, confidence, origin="ai_match", auto_threshold=auto_threshold,
                            media_files=media.parse_files(chosen["media_files"]))
        return MatchOutcome("queued", qid, reason)

    # ---------------- 手动路线 ----------------
    def manual_match(self, target_id: int, material_id: int, text: str | None = None,
                     account: sqlite3.Row | None = None) -> MatchOutcome:
        """人工在抓取记录里选定一条素材（可顺手改文案）→ 进待审核。account 不传时按来源规则的「回复账号」选。"""
        target, account, err, acc_note = self._prepare(target_id, account)
        if err:
            return MatchOutcome("no_match", None, err)
        with get_conn() as conn:
            mat = conn.execute("SELECT * FROM materials WHERE id=? AND deleted_at IS NULL", (material_id,)).fetchone()
        if mat is None:
            return MatchOutcome("no_match", None, "素材不存在或已在回收站")
        final = (text or "").strip() or mat["text"]
        reason = "人工选定素材" + (f"｜{acc_note}" if acc_note else "")
        final, len_note = textlimit.fit(final, account, self.llm, extract_must_include(final), target["lang"] or "")
        if len_note:
            reason += f"｜{len_note}"
        qid = self._enqueue(account["id"], target["id"], mat["id"], final, reason, 1.0, origin="manual",
                            media_files=media.parse_files(mat["media_files"]))
        return MatchOutcome("queued", qid, f"已按你选的素材生成待审核条目（{acc_note}）" if acc_note else "已按你选的素材生成待审核条目")

    def ai_write(self, target_id: int, brief: str, account: sqlite3.Row | None = None,
                 origin: str = "ai_write", acc_note: str = "", media_files: list[str] | None = None,
                 auto_threshold: float | None = None) -> MatchOutcome:
        """按创作要求让 LLM 现写回复 → 进待审核。account 不传时按来源规则的「回复账号」选；media_files 是随回复一起发的附件。
        auto_threshold：流水线传入的免审核阈值（None = 不免审核）；模型自评的 confidence ≥ 它就直接进待发送。"""
        target, account, err, note = self._prepare(target_id, account)
        if err:
            return MatchOutcome("no_match", None, err)
        acc_note = acc_note or note
        brief = (brief or "").strip()
        if not brief:
            return MatchOutcome("no_match", None, "请先写创作要求（主题、立场、必须带的链接或 @账号、语气）")
        must = extract_must_include(brief)
        try:
            res = self.llm.write_reply(target["text"], target["lang"] or "und", brief, must, textlimit.limit_for(account))
        except LLMError as e:
            self._mark_no_match(target["id"], f"AI 撰写失败：{e}")
            return MatchOutcome("no_match", None, f"AI 撰写失败：{e}")
        reason = "AI 按创作要求撰写" + (f"（已强制包含：{'、'.join(must)}）" if must else "") + "：" + (res.get("reason") or "")
        if acc_note:
            reason += f"｜{acc_note}"
        reply_text, len_note = textlimit.fit(res["reply_text"], account, self.llm, must, target["lang"] or "")
        if len_note:
            reason += f"｜{len_note}"
        qid = self._enqueue(account["id"], target["id"], None, reply_text, reason, float(res.get("confidence", 0.6)), origin=origin,
                            media_files=media_files, auto_threshold=auto_threshold)
        return MatchOutcome("queued", qid, reason)

    def rematch(self, target_id: int, *, expected_status: str | None = None,
                material_only: bool = False) -> MatchOutcome:
        """同一记录的批量/单条自动匹配互斥，避免重复生成。"""
        with self._rematch_lock:
            if target_id in self._rematching:
                return MatchOutcome("skipped", None, "该记录正在自动匹配")
            self._rematching.add(target_id)
        try:
            if expected_status is not None:
                with get_conn() as conn:
                    row = conn.execute("SELECT process_status FROM target_tweets WHERE id=?", (target_id,)).fetchone()
                if row is None or row["process_status"] != expected_status:
                    return MatchOutcome("skipped", None, "记录已删除或状态已改变")
            return self._rematch(target_id, expected_status=expected_status, material_only=material_only)
        except Exception as e:
            # 不让中途异常遗留为「待匹配」，也不覆盖并发操作已入队的结果。
            with get_conn() as conn:
                conn.execute("UPDATE target_tweets SET process_status='no_match', llm_relevance_reason=? "
                             "WHERE id=? AND process_status='new'", (f"自动匹配出错：{e}", target_id))
                conn.commit()
            raise
        finally:
            with self._rematch_lock:
                self._rematching.discard(target_id)

    def rematch_many(self, target_ids: list[int], progress, *, expected_status: str = "filtered",
                     material_only: bool = False) -> BatchMatchResult:
        """只处理用户勾选时的记录快照；复用手动匹配，始终生成待审核草稿。"""
        if expected_status not in ("filtered", "no_match"):
            raise ValueError("批量自动匹配只支持已过滤/未达标或达标但未生成回复的记录")
        ids = list(dict.fromkeys(target_ids))
        result = BatchMatchResult(total=len(ids))
        label = "素材库匹配" if material_only else "按来源规则自动匹配"
        for index, tid in enumerate(ids):
            progress(index / len(ids), f"{label}：第 {index + 1}/{len(ids)} 条（记录 #{tid}）\n{result.as_msg().splitlines()[0]}")
            try:
                outcome = self.rematch(tid, expected_status=expected_status, material_only=material_only)
                if outcome.status == "queued":
                    result.queued += 1
                elif outcome.status == "skipped":
                    result.skipped += 1
                    result.details.append(f"#{tid} 跳过：{outcome.reason}")
                else:
                    result.no_match += 1
                    result.details.append(f"#{tid} 未匹配：{outcome.reason}")
            except Exception as e:
                result.failed += 1
                result.details.append(f"#{tid} 出错：{e}")
            progress((index + 1) / len(ids), result.as_msg().splitlines()[0])
        return result

    def _rematch(self, target_id: int, *, expected_status: str | None = None,
                 material_only: bool = False) -> MatchOutcome:
        """默认沿用来源规则；material_only 只覆盖本次回复方式，账号配置仍沿用原规则。"""
        target, account, err, acc_note = self._prepare(target_id, None)
        if err:
            return MatchOutcome("no_match", None, err)
        with get_conn() as conn:
            condition = "process_status=?" if expected_status is not None else "process_status!='queued'"
            args = (target_id, expected_status) if expected_status is not None else (target_id,)
            changed = conn.execute("UPDATE target_tweets SET process_status='new', llm_relevance_reason=NULL "
                                   f"WHERE id=? AND {condition}", args).rowcount
            conn.commit()
        if not changed:
            return MatchOutcome("skipped", None, "记录已删除、状态已改变或已经进入任务队列")
        if material_only:
            return self._match_material(target, account, False, acc_note=acc_note, keep_original=True)
        cfg = load_source_cfg(target)
        if _cfg_get(cfg, "reply_mode", "material") == "manual":
            # 手动模式下点「重新匹配」= 用素材库自动配一次
            return self._match_material(target, account, bool(_cfg_get(cfg, "allow_polish", 0)), acc_note=acc_note)
        return self.run(target, account, cfg, pipeline=False)

    # ---------------- 内部 ----------------
    def _prepare(self, target_id: int, account: sqlite3.Row | None):
        """返回 (target, 回复账号, 错误, 账号说明)。account 传了就用它；没传按来源规则的「回复账号」选。"""
        with get_conn() as conn:
            target = conn.execute("SELECT * FROM target_tweets WHERE id=?", (target_id,)).fetchone()
            if target is None:
                return None, None, "记录不存在", ""
            if target["process_status"] == "queued":
                return None, None, "该推文已在任务队列中（先到任务队列删除/跳过那条，再重新处理）", ""
            dup = conn.execute("SELECT 1 FROM interactions WHERE action='reply' AND tweet_id=?",
                               (target["tweet_id"],)).fetchone()
            if dup:
                return None, None, "该推文已经回复过，不能再回复", ""
        if account is not None:
            return target, account, "", ""
        fallback = fallback_reply_account()
        if fallback is None:
            return None, None, "没有状态为「启用」的账号，请到「设置 → 账号」添加并启用一个", ""
        reply_acc, note = choose_reply_account(load_source_cfg(target), fallback)
        if reply_acc is None:
            return None, None, note, ""
        return target, reply_acc, "", note

    def _enqueue(self, account_id: int, target_id: int, material_id: int | None, text: str,
                 reason: str, confidence: float, origin: str, media_files: list[str] | None = None,
                 auto_threshold: float | None = None) -> int:
        """写入任务队列。auto_threshold 不为 None（这条规则 / 推主开了免审核）且置信度 ≥ 它 → 直接进待发送。"""
        ttl_hours = config.get_int("reply_ttl_hours", 48)
        expires_at = to_iso(datetime.now(timezone.utc) + timedelta(hours=ttl_hours))
        confidence = min(max(confidence, 0.0), 1.0)
        status, auto_ok = "pending", 0
        if auto_threshold is not None and confidence >= auto_threshold:
            status, auto_ok = "approved", 1
            reason += f"｜置信度 {confidence:.2f} ≥ {auto_threshold:.2f}，按这条规则的免审核设置直接进待发送"
        with get_conn() as conn:
            # 生成期间其他操作也可能处理同一记录；入队检查与写入必须原子执行。
            if not conn.in_transaction:   # 线程内复用连接：前面若有没 commit 的写入，再 BEGIN 会报错
                conn.execute("BEGIN IMMEDIATE")
            target = conn.execute("SELECT process_status FROM target_tweets WHERE id=?", (target_id,)).fetchone()
            if target is None:
                raise ValueError("抓取记录已删除，未创建回复草稿")
            if target["process_status"] == "queued":
                existing = conn.execute("SELECT id FROM review_queue WHERE target_tweet_id=? ORDER BY id DESC LIMIT 1",
                                        (target_id,)).fetchone()
                if existing:
                    conn.commit()
                    return existing["id"]
            cur = conn.execute(
                "INSERT INTO review_queue(account_id, action_type, target_tweet_id, material_id, "
                "final_text, final_media_files, llm_reason, llm_confidence, status, auto_approve, decided_at, expires_at, origin, created_at) "
                "VALUES (?, 'reply', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (account_id, target_id, material_id, text, media.dump_files(media_files), reason,
                 confidence, status, auto_ok, utcnow_iso() if auto_ok else None, expires_at, origin, utcnow_iso()),
            )
            qid = cur.lastrowid
            conn.execute("UPDATE target_tweets SET process_status='queued' WHERE id=?", (target_id,))
            conn.commit()
        return qid

    def _mark_no_match(self, target_id: int, reason: str) -> None:
        with get_conn() as conn:
            conn.execute(
                "UPDATE target_tweets SET process_status='no_match', llm_relevance_reason=? WHERE id=? AND process_status!='queued'",
                (reason, target_id),
            )
            conn.commit()


# 从推文里粗略推断场景标签，用来和素材的 scenario_tags 对上（通用场景，不绑定行业）
_TAG_KEYWORDS = {
    "cost": ["コスト", "料金", "高い", "安く", "成本", "太贵", "顶不住", "cost", "cheaper", "bill", "expensive"],
    "recommend": ["おすすめ", "探して", "求推荐", "有没有", "recommend", "anyone", "looking for"],
    "alternative": ["代替", "替代", "乗り換え", "alternative", "switch"],
    "compare": ["比較", "使い分け", "compare", "统一", "まとめ", "vs"],
}


def _to_float(v, default: float) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _infer_tags(text: str) -> list[str]:
    low = text.lower()
    tags = []
    for tag, kws in _TAG_KEYWORDS.items():
        if any(k.lower() in low for k in kws):
            tags.append(tag)
    return tags
