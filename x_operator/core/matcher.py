"""MatchEngine（design-v1.1 §7.3）：为一条命中推文挑最佳回复素材，写入审核队列。

流程：pick_candidates（同语言 active 的 reply 素材，标签有交集优先）→ LLM/启发式择优
→ 命中则写 review_queue(pending)、target 置 queued；跳过则 target 置 no_match。
LLM 异常按 no_match 处理（不阻断流水线）。
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from .. import config
from ..db.database import get_conn, utcnow_iso
from ..llm.client import LLMClient, LLMError


@dataclass(frozen=True)
class MatchOutcome:
    status: Literal["queued", "no_match"]
    queue_id: int | None
    reason: str


class MatchEngine:
    def __init__(self, llm: LLMClient):
        self.llm = llm

    def pick_candidates(self, lang: str, tags: list[str], limit: int = 10) -> list[sqlite3.Row]:
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM materials WHERE kind='reply' AND status='active' AND lang=? "
                "ORDER BY usage_count ASC, COALESCE(last_used_at,'') ASC",
                (lang,),
            ).fetchall()
        if not rows:
            return []
        tagset = set(t for t in tags if t)

        def overlap(m: sqlite3.Row) -> int:
            mtags = set(x for x in (m["scenario_tags"] or "").split(",") if x)
            return 1 if (tagset & mtags) else 0

        # 有交集优先，其次按 usage_count/last_used_at（rows 已排序）
        rows_sorted = sorted(rows, key=lambda m: (-overlap(m),))
        return rows_sorted[:limit]

    def run(self, target: sqlite3.Row, account: sqlite3.Row) -> MatchOutcome:
        lang = target["lang"] or "ja"
        # 标签来源：MVP 用 target 的关键词粗略推断（真实版可由搜索规则/LLM 提供）
        tags = _infer_tags(target["text"])
        candidates = self.pick_candidates(lang, tags)

        if not candidates:
            self._mark_no_match(target["id"], "无同语言可用回复素材")
            return MatchOutcome("no_match", None, "无同语言可用回复素材")

        cand_payload = [{"material_id": c["id"], "text": c["text"], "lang": c["lang"]} for c in candidates]
        try:
            decision = self.llm.match_reply(target["text"], lang, cand_payload)
        except LLMError as e:
            self._mark_no_match(target["id"], f"LLM 匹配失败：{e}")
            return MatchOutcome("no_match", None, f"LLM 匹配失败：{e}")

        threshold = config.get_float("match_confidence_threshold", 0.7)
        if decision.get("skip") or float(decision.get("confidence", 0)) < threshold:
            reason = decision.get("reason") or "置信度低于阈值"
            self._mark_no_match(target["id"], reason)
            return MatchOutcome("no_match", None, reason)

        reply_text = decision.get("reply_text") or ""
        material_id = decision.get("material_id")
        confidence = float(decision.get("confidence", 0))
        reason = decision.get("reason") or ""
        ttl_hours = config.get_int("reply_ttl_hours", 48)
        expires_at = (datetime.now(timezone.utc) + timedelta(hours=ttl_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")

        with get_conn() as conn:
            cur = conn.execute(
                "INSERT INTO review_queue(account_id, action_type, target_tweet_id, material_id, "
                "final_text, llm_reason, llm_confidence, status, expires_at, created_at) "
                "VALUES (?, 'reply', ?, ?, ?, ?, ?, 'pending', ?, ?)",
                (account["id"], target["id"], material_id, reply_text, reason, confidence,
                 expires_at, utcnow_iso()),
            )
            qid = cur.lastrowid
            conn.execute("UPDATE target_tweets SET process_status='queued' WHERE id=?", (target["id"],))
            conn.commit()
        return MatchOutcome("queued", qid, reason)

    def _mark_no_match(self, target_id: int, reason: str) -> None:
        with get_conn() as conn:
            conn.execute(
                "UPDATE target_tweets SET process_status='no_match', llm_relevance_reason=COALESCE(llm_relevance_reason, ?) WHERE id=?",
                (reason, target_id),
            )
            conn.commit()


_TAG_KEYWORDS = {
    "cost": ["コスト", "料金", "高い", "従量", "安く", "成本", "太贵", "顶不住", "cost", "cheaper", "bill"],
    "api": ["api", "API"],
    "model": ["モデル", "model", "llm", "LLM", "大模型"],
    "compare": ["比較", "使い分け", "compare", "统一", "まとめ"],
}


def _infer_tags(text: str) -> list[str]:
    low = text.lower()
    tags = []
    for tag, kws in _TAG_KEYWORDS.items():
        if any(k.lower() in low for k in kws):
            tags.append(tag)
    return tags
