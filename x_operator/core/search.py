"""SearchJob（design-v1.1 §7.4 / v1.0 §8.2）：语义搜索两级漏斗。

粗筛（关键词 query 抓取，自动带上规则选的语言）→ 去掉已抓过的、语言不符的、预检拦下的（这些不花 LLM）
→ 精筛（LLM 相关性打分，>= 规则达标分者）→ MatchEngine。
run_once(rule_ids=[...]) 只跑指定规则（规则卡片上的「运行此规则」）；不传则跑全部启用的规则。没有只看不存的预览模式。

每一条被挡下的推文都会写进 target_tweets，llm_relevance_reason 里写明「为什么」——
「抓取记录」页原样展示，用户不用猜过滤条件。
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

from ..adapters import factory
from ..adapters.base import TweetData, XClientError
from ..db.database import get_conn, utcnow_iso
from ..llm.client import LLMClient, LLMError
from . import budget
from .matcher import MatchEngine
from .monitor import (FILTER_REASONS, _log_read, _row_int, get_read_account,
                      precheck, read_is_billed, store_target)

LANG_LABEL = {"ja": "日语", "en": "英语", "zh": "中文", "ko": "韩语", "es": "西班牙语",
              "fr": "法语", "de": "德语", "pt": "葡萄牙语", "id": "印尼语", "th": "泰语"}

# X 官方「最近搜索」只能查 7 天；规则里填得再大也只能抓到这么多
OFFICIAL_SEARCH_MAX_HOURS = 168
# 观看量门槛开着时，为凑够「每次抓取条数」最多翻页扫描多少条：条数 × 倍数，封顶 SCAN_CAP，且不超过当日剩余读额度
SCAN_FACTOR = 10
SCAN_CAP = 500


def rule_langs(rule: sqlite3.Row | dict) -> list[str]:
    """规则的语言列表（存储为逗号分隔，如 'ja,en'）。"""
    return [x.strip() for x in (rule["lang"] or "").split(",") if x.strip()]


def langs_label(langs: list[str]) -> str:
    return " / ".join(LANG_LABEL.get(x, x) for x in langs) if langs else "不限"


_CJK_RE = re.compile(r"[぀-ヿ㐀-鿿가-힯]")


def _term(t: str) -> str:
    """单个关键词：含中日韩文字或空格的加引号做整词匹配；已经带引号/是操作符的原样。"""
    t = t.strip()
    if not t or t.startswith('"') or t.startswith("-") or ":" in t:
        return t
    if " " in t or _CJK_RE.search(t):
        return f'"{t}"'
    return t


def normalize_keywords(raw: str) -> str:
    """把「逗号分隔 = 任一命中」翻译成 X 语法。

    规则：用户写 `adult,nsfw,AI美女` 这种逗号列表（中英文逗号、顿号都行），且没有自己写 OR，
    就转成 `adult OR nsfw OR "AI美女"`；已经用了 OR / 括号 / 操作符的高级写法原样保留。"""
    q = (raw or "").strip()
    if not q:
        return q
    parts = [p for p in re.split(r"[,，、\n]+", q) if p.strip()]
    if len(parts) <= 1 or " OR " in q or "(" in q:
        return q
    return " OR ".join(_term(p) for p in parts)


def effective_query(rule: sqlite3.Row | dict) -> str:
    """实际发给 X 的查询：逗号列表转 OR；用户没写 lang: 时按规则选的语言补上 (lang:ja OR lang:en)；
    默认排除转推。"""
    q = normalize_keywords(rule["keyword_query"])
    langs = rule_langs(rule)
    if " OR " in q and not q.startswith("("):
        q = f"({q})"
    if langs and "lang:" not in q:
        q += " (" + " OR ".join(f"lang:{x}" for x in langs) + ")"
    if "is:retweet" not in q:
        q += " -is:retweet"
    return q


def coerce_score(v) -> int:
    """LLM 返回的分数可能是 8、8.0、"8"、"8/10"、"八"……只认得出数字的，认不出记 0，钳到 0-10。"""
    if isinstance(v, bool):
        return 0
    if isinstance(v, (int, float)):
        n = int(round(v))
    else:
        m = re.search(r"\d+(?:\.\d+)?", str(v or ""))
        n = int(round(float(m.group(0)))) if m else 0
    return max(0, min(10, n))


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
    prefiltered: str | None = None   # 不为空 = 没送去打分就被挡下了（语言不符 / 预检），值是中文原因


class SearchJob:
    def __init__(self, match_engine: MatchEngine, llm: LLMClient):
        self.match = match_engine
        self.llm = llm

    def run_rule(self, rule: sqlite3.Row, account: sqlite3.Row,
                 notes: list[str] | None = None, progress=None) -> list[ScoredCandidate]:
        """抓取 + 预过滤 + 打分。返回按推文 id 升序的候选（含被预过滤的，prefiltered 字段说明原因）。
        notes：可选，运行中的提示（比如回溯被钳到 7 天）追加到这里。
        progress(0~1, 文字)：可选，进度回调（抓取 → 0.3，打分完 → 0.6，剩下留给生成回复）。"""
        def _p(frac: float, text: str) -> None:
            if progress:
                progress(frac, text)

        client = factory.get_client(account)
        lookback_h = _row_int(rule, "lookback_hours", 24)
        if account["access_type"] == "official" and lookback_h > OFFICIAL_SEARCH_MAX_HOURS:
            if notes is not None:
                notes.append(f"规则「{rule['name']}」首次回溯 {lookback_h} 小时超过官方 API 上限，按 {OFFICIAL_SEARCH_MAX_HOURS} 小时（7 天）抓")
            lookback_h = OFFICIAL_SEARCH_MAX_HOURS
        # 有游标：抓上次之后的全部；没有游标（首次/重置后）：只抓最近 lookback_hours 小时
        start_time = None
        if not rule["newest_id_cursor"] and lookback_h:
            start_time = datetime.now(timezone.utc) - timedelta(hours=lookback_h)
        max_results = int(rule["max_results_per_run"])
        min_views = _row_int(rule, "min_views", 0)
        scan_limit = 0
        if min_views:
            scan_limit = min(SCAN_CAP, max_results * SCAN_FACTOR)
            if read_is_billed(account):
                b = budget.current()
                if b.daily_budget > 0:
                    scan_limit = max(max_results, min(scan_limit, b.remaining))
        _p(0.05, f"规则「{rule['name']}」：正在从 X 抓取（{'游标之后的新推文' if rule['newest_id_cursor'] else f'最近 {lookback_h} 小时'}"
                 + (f"，观看量 ≥ {min_views}，不够就翻页，最多扫 {scan_limit} 条" if min_views else "") + "）…")
        result = client.search_recent(effective_query(rule), since_id=rule["newest_id_cursor"],
                                      start_time=start_time, max_results=max_results,
                                      min_views=min_views, scan_limit=scan_limit)
        _log_read(account["id"], client.api_kind, "search_recent", result.reads_consumed)
        tweets = result.tweets
        if min_views and notes is not None and result.scanned:
            tip = (f"规则「{rule['name']}」：扫描 {result.scanned} 条，观看量低于 {min_views} 的 {result.dropped_low_views} 条已跳过（不入库），"
                   f"达标 {len(tweets)} 条")
            if len(tweets) < max_results and scan_limit and result.scanned >= scan_limit:
                tip += f"；已到本次扫描上限 {scan_limit} 条，想多抓可调低观看量门槛或调大「每次抓取条数」"
            notes.append(tip)
        _p(0.3, f"规则「{rule['name']}」：抓到 {len(tweets)} 条" + (f"（扫描 {result.scanned} 条）" if min_views else "") + "，正在去重和预检…")
        if start_time:
            tweets = [t for t in tweets if t.created_at >= start_time]
        if tweets:
            # 已经抓过的（别的规则/上一轮存过）不再打分，省 LLM
            marks = ",".join("?" * len(tweets))
            with get_conn() as conn:
                known = {r["tweet_id"] for r in conn.execute(
                    f"SELECT tweet_id FROM target_tweets WHERE tweet_id IN ({marks})", [t.tweet_id for t in tweets])}
            tweets = [t for t in tweets if t.tweet_id not in known]
        if not tweets:
            return []

        langs = rule_langs(rule)
        max_age = None if rule["newest_id_cursor"] else lookback_h
        pre: list[ScoredCandidate] = []
        to_score: list[TweetData] = []
        for t in tweets:
            # 语言不符（X 偶尔会返回别的语言）—— 不花 LLM
            if langs and t.lang and t.lang not in ("und", "qme", "zxx") and t.lang not in langs:
                pre.append(ScoredCandidate(t, 0, "", f"推文语言是 {LANG_LABEL.get(t.lang, t.lang)}，不在规则选的语言（{langs_label(langs)}）内"))
                continue
            code = precheck(t, account["handle"], max_age_h=max_age)
            if code:
                pre.append(ScoredCandidate(t, 0, "", "预检拦下：" + FILTER_REASONS.get(code, code)))
                continue
            to_score.append(t)

        scored: list[ScoredCandidate] = []
        if to_score:
            _p(0.4, f"规则「{rule['name']}」：{len(to_score)} 条送去打分（{'LLM' if self.llm.configured else '关键词粗估'}），预检挡下 {len(pre)} 条…")
            payload = [{"tweet_id": t.tweet_id, "author_handle": t.author_handle, "text": t.text} for t in to_score]
            try:
                scores = self.llm.score_relevance(rule["semantic_criteria"], payload)
            except LLMError as e:
                scores = self.llm.score_relevance_heuristic(payload)
                for s in scores:
                    s["reason"] = f"LLM 调用失败（{str(e)[:60]}），改用关键词粗略打分：" + s.get("reason", "")
            # LLM 可能把 tweet_id 当成数字返回；分数也可能是 "8/10" 之类
            score_map = {str(s.get("tweet_id", "")).strip(): s for s in (scores or []) if isinstance(s, dict)}
            for t in to_score:
                s = score_map.get(t.tweet_id, {"score": 0, "reason": "LLM 没有返回这条的打分"})
                scored.append(ScoredCandidate(t, coerce_score(s.get("score", 0)), str(s.get("reason", "") or "")))
        out = pre + scored
        out.sort(key=lambda c: int(c.tweet.tweet_id) if c.tweet.tweet_id.isdigit() else 0)
        _p(0.6, f"规则「{rule['name']}」：打分完成，正在生成回复草稿…")
        return out

    def run_once(self, auto: bool = False, rule_ids: list[int] | None = None, progress=None) -> SearchStats:
        """auto=True 表示后台自动轮询（读额度熔断更保守）；手动按钮触发传 False。
        rule_ids：只跑这些规则（停用的也跑——用户明确点了这一条）；None = 全部启用的规则。
        progress(0~1, 文字)：可选进度回调，UI 进度框用。"""
        stats = SearchStats()
        account = get_read_account()
        if account is None:
            stats.notes.append("没有状态为「启用」的账号，无法搜索。请到「设置 → 账号」添加并启用一个账号")
            return stats
        if read_is_billed(account):
            denied = budget.current().allow(auto)
            if denied:
                stats.notes.append(denied)
                return stats
        with get_conn() as conn:
            if rule_ids:
                marks = ",".join("?" * len(rule_ids))
                rules = conn.execute(f"SELECT * FROM search_rules WHERE id IN ({marks})", list(rule_ids)).fetchall()
            else:
                rules = conn.execute("SELECT * FROM search_rules WHERE enabled=1").fetchall()
        if not rules:
            stats.notes.append("没有启用的搜索规则。请到「搜索规则」页新建或启用" if not rule_ids else "规则不存在（可能已被删除）")
            return stats

        llm_ok = self.llm.configured
        below = 0          # 因打分低于达标分而被挡的数量
        min_scores: list[int] = []
        total = len(rules)

        def _p(i: int, sub: float, text: str) -> None:
            if progress:
                progress((i + sub) / total, f"（{i + 1}/{total}）" + text)

        for i, rule in enumerate(rules):
            stats.rules_run += 1
            min_scores.append(int(rule["min_llm_score"]))
            try:
                scored = self.run_rule(rule, account, notes=stats.notes,
                                       progress=lambda sub, text, i=i: _p(i, sub, text))
                stats.tweets_fetched += len(scored)
                newest_id = None
                for k, cand in enumerate(scored):
                    _p(i, 0.6 + 0.4 * k / max(1, len(scored)), f"规则「{rule['name']}」：处理第 {k + 1}/{len(scored)} 条…")
                    t = cand.tweet
                    if t.tweet_id.isdigit() and (newest_id is None or int(t.tweet_id) > int(newest_id)):
                        newest_id = t.tweet_id
                    if cand.prefiltered:
                        store_target(t, "search", rule["id"], process_status="filtered", reason=cand.prefiltered)
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
                    tid = store_target(t, "search", rule["id"], process_status="new",
                                       score=cand.score, reason=cand.reason)
                    if tid is None:
                        continue
                    with get_conn() as conn:
                        target = conn.execute("SELECT * FROM target_tweets WHERE id=?", (tid,)).fetchone()
                    outcome = self.match.run(target, account, cfg=rule)
                    if outcome.status == "queued":
                        stats.queued += 1
                    else:
                        stats.no_match += 1
                # 推进游标
                with get_conn() as conn:
                    if newest_id:
                        conn.execute("UPDATE search_rules SET newest_id_cursor=?, last_run_at=? WHERE id=?",
                                     (newest_id, utcnow_iso(), rule["id"]))
                    else:
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
        stats.notes.append(f"本次用 @{account['handle']} 抓取（{'官方 API，计费' if read_is_billed(account) else '小号通道，不计费'}）")
        if progress:
            progress(1.0, "完成")
        return stats


def _kind(account: sqlite3.Row) -> str:
    return "x_official" if account["access_type"] == "official" else "x_unofficial"
