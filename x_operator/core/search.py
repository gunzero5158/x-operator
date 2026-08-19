"""SearchJob（design-v1.1 §7.4 / v1.0 §8.2）：语义搜索两级漏斗。

粗筛（关键词 query 抓取）→ 精筛（LLM 相关性打分，>= 规则阈值者）→ MatchEngine。
dry_run=True：拉取 + 打分后直接返回，不写库、不推进游标、不进匹配（UI 试运行按钮用）。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import NamedTuple

from ..adapters import factory
from ..adapters.base import TweetData, XClientError
from ..db.database import get_conn, utcnow_iso
from ..llm.client import LLMClient, LLMError
from .matcher import MatchEngine
from .monitor import _log_read, get_primary_account, precheck, store_target


@dataclass
class SearchStats:
    rules_run: int = 0
    tweets_fetched: int = 0
    queued: int = 0
    filtered: int = 0
    errors: int = 0

    def as_msg(self) -> str:
        return (f"搜索完成：运行 {self.rules_run} 条规则，拉取 {self.tweets_fetched} 条，"
                f"入队 {self.queued}，过滤/未达标 {self.filtered}，错误 {self.errors}")


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
            return stats
        with get_conn() as conn:
            rules = conn.execute("SELECT * FROM search_rules WHERE enabled=1").fetchall()

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
                                     score=cand.score, reason=pre)
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
                        stats.filtered += 1
                # 推进游标
                if newest_id:
                    with get_conn() as conn:
                        conn.execute("UPDATE search_rules SET newest_id_cursor=?, last_run_at=? WHERE id=?",
                                     (newest_id, utcnow_iso(), rule["id"]))
                        conn.commit()
            except XClientError as e:
                stats.errors += 1
                _log_read(account["id"], "x_mock", "search_recent", 0, success=False, error=str(e))
            except Exception as e:
                stats.errors += 1
                _log_read(account["id"], "x_mock", "search_recent", 0, success=False, error=str(e))
        return stats
