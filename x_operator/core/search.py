"""SearchJob（design-v1.1 §7.4 / v1.0 §8.2）：语义搜索两级漏斗。

粗筛（关键词 query 抓取）→ 精筛（LLM 相关性打分，>= 规则阈值者）→ MatchEngine。
dry_run=True：拉取 + 打分后直接返回，不写库、不推进游标、不进匹配（UI 试运行按钮用）。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import NamedTuple

from ..adapters import factory
from ..adapters.base import TweetData, XClientError
from ..db.database import get_conn, utcnow_iso
from ..llm.client import LLMClient, LLMError
from .matcher import MatchEngine
from .monitor import (FILTER_REASONS, _log_read, get_primary_account, precheck,
                      store_target)


@dataclass
class SearchStats:
    rules_run: int = 0
    tweets_fetched: int = 0
    queued: int = 0
    no_match: int = 0
    filtered: int = 0
    errors: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.errors == 0 and not (self.rules_run == 0 and self.notes)

    def as_msg(self) -> str:
        head = (f"搜索完成：运行 {self.rules_run} 条规则，拉取 {self.tweets_fetched} 条，"
                f"入队 {self.queued}，未匹配 {self.no_match}，过滤/未达标 {self.filtered}，错误 {self.errors}")
        if self.notes:
            head += "。" + "；".join(self.notes[:3])
        return head


class ScoredCandidate(NamedTuple):
    tweet: TweetData
    score: int
    reason: str


class SearchJob:
    def __init__(self, match_engine: MatchEngine, llm: LLMClient):
        self.match = match_engine
        self.llm = llm

    def run_rule(self, rule: sqlite3.Row, account: sqlite3.Row, dry_run: bool = False) -> list[ScoredCandidate]:
        client = factory.get_client(account)
        result = client.search_recent(rule["keyword_query"], since_id=rule["newest_id_cursor"],
                                       max_results=rule["max_results_per_run"])
        _log_read(account["id"], client.api_kind, "search_recent", result.reads_consumed)
        if not result.tweets:
            return []
        payload = [{"tweet_id": t.tweet_id, "author_handle": t.author_handle, "text": t.text}
                   for t in result.tweets]
        try:
            scores = self.llm.score_relevance(rule["semantic_criteria"], payload)
        except LLMError:
            scores = self.llm.score_relevance_heuristic(payload)
        score_map = {s["tweet_id"]: s for s in scores}
        scored = []
        for t in result.tweets:
            s = score_map.get(t.tweet_id, {"score": 0, "reason": "未打分"})
            scored.append(ScoredCandidate(t, int(s.get("score", 0)), s.get("reason", "")))
        return scored

    def run_once(self) -> SearchStats:
        stats = SearchStats()
        account = get_primary_account()
        if account is None:
            stats.notes.append("没有状态为「启用」的账号，无法搜索。请到「设置 → 账号」添加并启用一个账号")
            return stats
        with get_conn() as conn:
            rules = conn.execute("SELECT * FROM search_rules WHERE enabled=1").fetchall()
        if not rules:
            stats.notes.append("没有启用的搜索规则。请到「搜索规则」页新建或启用")
            return stats

        for rule in rules:
            stats.rules_run += 1
            try:
                scored = self.run_rule(rule, account, dry_run=False)
                stats.tweets_fetched += len(scored)
                newest_id = None
                for cand in scored:
                    t = cand.tweet
                    if newest_id is None or int(t.tweet_id) > int(newest_id):
                        newest_id = t.tweet_id
                    # 达标判断
                    if cand.score < rule["min_llm_score"]:
                        store_target(t, "search", rule["id"], process_status="filtered",
                                     score=cand.score, reason=cand.reason)
                        stats.filtered += 1
                        continue
                    # 预检
                    pre = precheck(t, account["handle"])
                    if pre:
                        store_target(t, "search", rule["id"], process_status="filtered",
                                     score=cand.score, reason=FILTER_REASONS.get(pre, pre))
                        stats.filtered += 1
                        continue
                    tid = store_target(t, "search", rule["id"], process_status="new",
                                       score=cand.score, reason=cand.reason)
                    if tid is None:
                        continue
                    with get_conn() as conn:
                        target = conn.execute("SELECT * FROM target_tweets WHERE id=?", (tid,)).fetchone()
                    outcome = self.match.run(target, account)
                    if outcome.status == "queued":
                        stats.queued += 1
                    else:
                        stats.no_match += 1
                # 推进游标
                if newest_id:
                    with get_conn() as conn:
                        conn.execute("UPDATE search_rules SET newest_id_cursor=?, last_run_at=? WHERE id=?",
                                     (newest_id, utcnow_iso(), rule["id"]))
                        conn.commit()
            except XClientError as e:
                stats.errors += 1
                stats.notes.append(f"规则「{rule['name']}」：{e}")
                _log_read(account["id"], _kind(account), "search_recent", 0, success=False, error=str(e))
            except Exception as e:
                stats.errors += 1
                stats.notes.append(f"规则「{rule['name']}」：{e}")
                _log_read(account["id"], _kind(account), "search_recent", 0, success=False, error=str(e))
        if stats.tweets_fetched == 0 and stats.errors == 0 and stats.rules_run:
            stats.notes.append("自上次游标之后没有新推文（可在规则卡片上「重置游标」）")
        return stats


def _kind(account: sqlite3.Row) -> str:
    from .. import config
    if config.get_bool("dry_run", True):
        return "x_mock"
    return "x_official" if account["access_type"] == "official" else "x_unofficial"
