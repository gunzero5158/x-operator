"""SearchJob（design-v1.1 §7.4 / v1.0 §8.2）：语义搜索两级漏斗。

粗筛（关键词 query 抓取，自动带上规则选的语言）→ 精筛（LLM 相关性打分，>= 规则达标分者）→ MatchEngine。
preview=True：拉取 + 打分后直接返回，不写库、不推进游标、不进匹配（UI「试运行」按钮用）。

每一条被挡下的推文都会写进 target_tweets，llm_relevance_reason 里写明「为什么」——
「抓取记录」页原样展示，用户不用猜过滤条件。
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

LANG_LABEL = {"ja": "日语", "en": "英语", "zh": "中文", "ko": "韩语", "es": "西班牙语",
              "fr": "法语", "de": "德语", "pt": "葡萄牙语", "id": "印尼语", "th": "泰语"}


def rule_langs(rule: sqlite3.Row | dict) -> list[str]:
    """规则的语言列表（存储为逗号分隔，如 'ja,en'）。"""
    return [x.strip() for x in (rule["lang"] or "").split(",") if x.strip()]


def langs_label(langs: list[str]) -> str:
    return " / ".join(LANG_LABEL.get(x, x) for x in langs) if langs else "不限"


def effective_query(rule: sqlite3.Row | dict) -> str:
    """实际发给 X 的查询：用户没写 lang: 时，自动按规则选的语言补上 (lang:ja OR lang:en)。"""
    q = (rule["keyword_query"] or "").strip()
    langs = rule_langs(rule)
    if langs and "lang:" not in q:
        q += " (" + " OR ".join(f"lang:{x}" for x in langs) + ")"
    return q


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
                f"进审核队列 {self.queued}，达标但没配到素材 {self.no_match}，未达标/被过滤 {self.filtered}，错误 {self.errors}")
        if self.notes:
            head += "。\n" + "\n".join(self.notes[:4])
        return head


class ScoredCandidate(NamedTuple):
    tweet: TweetData
    score: int
    reason: str


class SearchJob:
    def __init__(self, match_engine: MatchEngine, llm: LLMClient):
        self.match = match_engine
        self.llm = llm

    def run_rule(self, rule: sqlite3.Row, account: sqlite3.Row, preview: bool = False) -> list[ScoredCandidate]:
        client = factory.get_client(account)
        result = client.search_recent(effective_query(rule), since_id=rule["newest_id_cursor"],
                                      max_results=rule["max_results_per_run"])
        _log_read(account["id"], client.api_kind, "search_recent", result.reads_consumed)
        if not result.tweets:
            return []
        payload = [{"tweet_id": t.tweet_id, "author_handle": t.author_handle, "text": t.text}
                   for t in result.tweets]
        try:
            scores = self.llm.score_relevance(rule["semantic_criteria"], payload)
        except LLMError as e:
            scores = self.llm.score_relevance_heuristic(payload)
            for s in scores:
                s["reason"] = f"LLM 调用失败（{str(e)[:60]}），改用关键词粗略打分：" + s.get("reason", "")
        score_map = {s["tweet_id"]: s for s in scores}
        scored = []
        for t in result.tweets:
            s = score_map.get(t.tweet_id, {"score": 0, "reason": "LLM 没有返回这条的打分"})
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

        llm_ok = self.llm.configured
        below = 0          # 因打分低于达标分而被挡的数量
        min_scores: list[int] = []
        for rule in rules:
            stats.rules_run += 1
            min_scores.append(int(rule["min_llm_score"]))
            langs = rule_langs(rule)
            try:
                scored = self.run_rule(rule, account, preview=False)
                stats.tweets_fetched += len(scored)
                newest_id = None
                for cand in scored:
                    t = cand.tweet
                    if newest_id is None or int(t.tweet_id) > int(newest_id):
                        newest_id = t.tweet_id
                    # 语言不符（X 偶尔会返回别的语言）
                    if langs and t.lang and t.lang not in ("und", "qme", "zxx") and t.lang not in langs:
                        store_target(t, "search", rule["id"], process_status="filtered", score=cand.score,
                                     reason=f"推文语言是 {LANG_LABEL.get(t.lang, t.lang)}，不在规则选的语言（{langs_label(langs)}）内")
                        stats.filtered += 1
                        continue
                    # 达标判断
                    if cand.score < rule["min_llm_score"]:
                        store_target(t, "search", rule["id"], process_status="filtered", score=cand.score,
                                     reason=f"相关性 {cand.score}/10，低于规则「{rule['name']}」的达标分 {rule['min_llm_score']}。"
                                            f"打分理由：{cand.reason}")
                        stats.filtered += 1
                        below += 1
                        continue
                    # 预检
                    pre = precheck(t, account["handle"])
                    if pre:
                        store_target(t, "search", rule["id"], process_status="filtered", score=cand.score,
                                     reason="预检拦下：" + FILTER_REASONS.get(pre, pre) + f"。打分理由：{cand.reason}")
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
                else:
                    with get_conn() as conn:
                        conn.execute("UPDATE search_rules SET last_run_at=? WHERE id=?", (utcnow_iso(), rule["id"]))
                        conn.commit()
            except (XClientError, ValueError) as e:
                stats.errors += 1
                stats.notes.append(f"规则「{rule['name']}」：{e}")
                _log_read(account["id"], _kind(account), "search_recent", 0, success=False, error=str(e))
            except Exception as e:
                stats.errors += 1
                stats.notes.append(f"规则「{rule['name']}」：{e}")
                _log_read(account["id"], _kind(account), "search_recent", 0, success=False, error=str(e))

        if stats.tweets_fetched == 0 and stats.errors == 0 and stats.rules_run:
            stats.notes.append("自上次游标之后没有新推文（可在规则卡片上「重置游标」重新抓最近的）")
        if stats.tweets_fetched and stats.queued == 0 and stats.no_match == 0 and below:
            tip = f"这次抓到的推文全部未达标（相关性打分低于达标分 {min(min_scores)}）。"
            if not llm_ok:
                tip += "当前没配置 LLM，打分只是关键词粗估，普遍偏低——建议到「设置 → LLM」配置网关，或先把规则达标分调到 4~5。"
            else:
                tip += "可到「搜索规则」把达标分调低，或放宽语义条件。"
            stats.notes.append(tip)
        if stats.tweets_fetched:
            stats.notes.append("每条推文的打分和被过滤的原因都在「抓取记录」页")
        return stats


def _kind(account: sqlite3.Row) -> str:
    return "x_official" if account["access_type"] == "official" else "x_unofficial"
