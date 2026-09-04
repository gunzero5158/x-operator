"""MatchEngine（design-v1.1 §7.3）：为一条命中推文生成回复草稿，写入审核队列。

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
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from .. import config
from ..db.database import get_conn, to_iso, utcnow_iso
from ..llm.client import LLMClient, LLMError
from . import media
from .accounts import choose_reply_account

REPLY_MODE_LABEL = {"material": "匹配素材库", "ai_write": "AI 按要求创作", "manual": "只抓取，手动处理"}


@dataclass(frozen=True)
class MatchOutcome:
    status: Literal["queued", "no_match"]
    queue_id: int | None
    reason: str


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

    # ---------------- 素材候选 ----------------
    def pick_candidates(self, lang: str, tags: list[str], limit: int = 15) -> tuple[list[sqlite3.Row], bool]:
        """返回 (候选列表, 是否同语言)。优先同语言的启用回复素材；一条都没有就退回全部语言（宁可给一条让人审，也不空手）。"""
        base = "SELECT * FROM materials WHERE kind='reply' AND status='active' AND deleted_at IS NULL"
        with get_conn() as conn:
            rows = conn.execute(base + " AND lang=? ORDER BY usage_count ASC, COALESCE(last_used_at,'') ASC", (lang,)).fetchall()
            same_lang = bool(rows)
            if not rows:
                rows = conn.execute(base + " ORDER BY usage_count ASC, COALESCE(last_used_at,'') ASC").fetchall()
        if not rows:
            return [], same_lang
        tagset = set(t for t in tags if t)

        def overlap(m: sqlite3.Row) -> int:
            mtags = set(x for x in (m["scenario_tags"] or "").split(",") if x)
            return 1 if (tagset & mtags) else 0

        rows_sorted = sorted(rows, key=lambda m: (-overlap(m),))
        return rows_sorted[:limit], same_lang

    # ---------------- 自动路线 ----------------
    def run(self, target: sqlite3.Row, account: sqlite3.Row, cfg: sqlite3.Row | None = None) -> MatchOutcome:
        """account = 抓取用的账号；真正用哪个账号回复由规则/推主的「回复账号」决定（见 core/accounts.py）。"""
        if cfg is None:
            cfg = load_source_cfg(target)
        mode = _cfg_get(cfg, "reply_mode", "material")
        if mode == "manual":
            self._mark_no_match(target["id"], "规则设置为「只抓取，手动处理」：请在这里点「选素材」或「AI 撰写」")
            return MatchOutcome("no_match", None, "等待手动处理")
        reply_acc, acc_note = choose_reply_account(cfg, account)
        if mode == "ai_write":
            brief = (_cfg_get(cfg, "ai_brief", "") or "").strip()
            if not brief:
                self._mark_no_match(target["id"], "规则选了「AI 按要求创作」但没填创作要求，请编辑规则补上")
                return MatchOutcome("no_match", None, "缺少创作要求")
            return self.ai_write(target["id"], brief, account=reply_acc, origin="ai_write", acc_note=acc_note)
        return self._match_material(target, reply_acc, bool(_cfg_get(cfg, "allow_polish", 0)), acc_note=acc_note)

    def _match_material(self, target: sqlite3.Row, account: sqlite3.Row, allow_polish: bool,
                        acc_note: str = "") -> MatchOutcome:
        """「宽进」：只要素材库里有启用的回复素材，就一定给出一条草稿进待审核——AI 择优；AI 拒绝/出错/说跳过/信心太低时
        退回到规则挑选（同语言里用得最少的一条），理由里写明，让审核的人知道这条是兜底出来的。"""
        lang = target["lang"] or "ja"
        tags = _infer_tags(target["text"])
        candidates, same_lang = self.pick_candidates(lang, tags)
        if not candidates:
            reason = "素材库里没有任何状态为「启用」的回复素材。到素材库添加（或用「AI 生成素材」）后再点「自动匹配」，也可在这里「AI 撰写」"
            self._mark_no_match(target["id"], reason)
            return MatchOutcome("no_match", None, reason)
        lang_note = "" if same_lang else f"（素材库没有「{lang}」语言的回复素材，这次从全部语言里挑的，审核时注意语言）"

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
        qid = self._enqueue(account["id"], target["id"], chosen["id"], reply_text, reason, confidence, origin="ai_match",
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
        qid = self._enqueue(account["id"], target["id"], mat["id"], final, reason, 1.0, origin="manual",
                            media_files=media.parse_files(mat["media_files"]))
        return MatchOutcome("queued", qid, f"已按你选的素材生成待审核条目（{acc_note}）" if acc_note else "已按你选的素材生成待审核条目")

    def ai_write(self, target_id: int, brief: str, account: sqlite3.Row | None = None,
                 origin: str = "ai_write", acc_note: str = "", media_files: list[str] | None = None) -> MatchOutcome:
        """按创作要求让 LLM 现写回复 → 进待审核。account 不传时按来源规则的「回复账号」选；media_files 是随回复一起发的附件。"""
        target, account, err, note = self._prepare(target_id, account)
        if err:
            return MatchOutcome("no_match", None, err)
        acc_note = acc_note or note
        brief = (brief or "").strip()
        if not brief:
            return MatchOutcome("no_match", None, "请先写创作要求（主题、立场、必须带的链接或 @账号、语气）")
        must = extract_must_include(brief)
        try:
            res = self.llm.write_reply(target["text"], target["lang"] or "und", brief, must)
        except LLMError as e:
            self._mark_no_match(target["id"], f"AI 撰写失败：{e}")
            return MatchOutcome("no_match", None, f"AI 撰写失败：{e}")
        reason = "AI 按创作要求撰写" + (f"（已强制包含：{'、'.join(must)}）" if must else "") + "：" + (res.get("reason") or "")
        if acc_note:
            reason += f"｜{acc_note}"
        qid = self._enqueue(account["id"], target["id"], None, res["reply_text"], reason, 0.9, origin=origin,
                            media_files=media_files)
        return MatchOutcome("queued", qid, reason)

    def rematch(self, target_id: int) -> MatchOutcome:
        """「抓取记录」页的重新匹配：按来源规则的回复方式再跑一次。"""
        target, account, err, acc_note = self._prepare(target_id, None)
        if err:
            return MatchOutcome("no_match", None, err)
        with get_conn() as conn:
            conn.execute("UPDATE target_tweets SET process_status='new', llm_relevance_reason=NULL WHERE id=?", (target_id,))
            conn.commit()
        cfg = load_source_cfg(target)
        if _cfg_get(cfg, "reply_mode", "material") == "manual":
            # 手动模式下点「重新匹配」= 用素材库自动配一次
            return self._match_material(target, account, bool(_cfg_get(cfg, "allow_polish", 0)), acc_note=acc_note)
        return self.run(target, account, cfg)

    # ---------------- 内部 ----------------
    def _prepare(self, target_id: int, account: sqlite3.Row | None):
        """返回 (target, 回复账号, 错误, 账号说明)。account 传了就用它；没传按来源规则的「回复账号」选。"""
        from .monitor import get_primary_account
        with get_conn() as conn:
            target = conn.execute("SELECT * FROM target_tweets WHERE id=?", (target_id,)).fetchone()
            if target is None:
                return None, None, "记录不存在", ""
            if target["process_status"] == "queued":
                return None, None, "该推文已在审核队列中（先到审核队列删除/跳过那条，再重新处理）", ""
            dup = conn.execute("SELECT 1 FROM interactions WHERE action='reply' AND tweet_id=?",
                               (target["tweet_id"],)).fetchone()
            if dup:
                return None, None, "该推文已经回复过，不能再回复", ""
        if account is not None:
            return target, account, "", ""
        fallback = get_primary_account()
        if fallback is None:
            return None, None, "没有状态为「启用」的账号", ""
        reply_acc, note = choose_reply_account(load_source_cfg(target), fallback)
        return target, reply_acc, "", note

    def _enqueue(self, account_id: int, target_id: int, material_id: int | None, text: str,
                 reason: str, confidence: float, origin: str, media_files: list[str] | None = None) -> int:
        ttl_hours = config.get_int("reply_ttl_hours", 48)
        expires_at = to_iso(datetime.now(timezone.utc) + timedelta(hours=ttl_hours))
        with get_conn() as conn:
            cur = conn.execute(
                "INSERT INTO review_queue(account_id, action_type, target_tweet_id, material_id, "
                "final_text, final_media_files, llm_reason, llm_confidence, status, expires_at, origin, created_at) "
                "VALUES (?, 'reply', ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)",
                (account_id, target_id, material_id, text, media.dump_files(media_files), reason,
                 min(max(confidence, 0.0), 1.0), expires_at, origin, utcnow_iso()),
            )
            qid = cur.lastrowid
            conn.execute("UPDATE target_tweets SET process_status='queued' WHERE id=?", (target_id,))
            conn.commit()
        return qid

    def _mark_no_match(self, target_id: int, reason: str) -> None:
        with get_conn() as conn:
            conn.execute(
                "UPDATE target_tweets SET process_status='no_match', llm_relevance_reason=? WHERE id=?",
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
