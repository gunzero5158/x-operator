"""LLMClient：OpenAI 兼容网关调用（design-v1.1 §6.0/§7.7）+ 离线启发式兜底。

关键设计（为「立即可测」服务）：
- 未配置 base_url/api_key 时，进入 heuristic 模式：用关键词规则给出相关性/匹配结果，
  让整条流水线在完全离线、零成本下也能演示。配置了网关后自动切换为真实 LLM。
- chat_json 做 JSON 抽取容错：截取首个 { 到末个 }，失败重试 1 次，仍失败抛 LLMFormatError。
- 每次调用写 action_log(api_kind='llm')，供仪表盘统计。
"""
from __future__ import annotations

import json
import re
import time

import httpx

from .. import config
from ..db.database import get_conn, utcnow_iso
from . import prompts


class LLMError(Exception):
    pass


class LLMFormatError(LLMError):
    pass


# 命中即视为正例线索的关键词（启发式兜底用；覆盖中日英常见「成本痛点」表达）
_POSITIVE_HINTS = ["高い", "コスト", "料金", "従量", "安く", "厳しい", "代替", "まとめ",
                   "顶不住", "太贵", "成本", "便宜", "按量", "统一",
                   "expensive", "cheaper", "bill", "cost", "afford", "gateway"]
# 命中即判为噪声（降分）
_NEGATIVE_HINTS = ["ニュース", "求人", "募集", "tutorial", "guide", "リリース", "#pr",
                   "イベント", "开课", "教程", "招聘", "新闻", "发布", "news", "hiring", "job"]


class LLMClient:
    def __init__(self):
        pass

    @property
    def configured(self) -> bool:
        return bool(config.get("llm_base_url") and config.get("llm_api_key"))

    # ---------------- 真实网关调用 ----------------
    def chat_json(self, scene: str, messages: list[dict], required_keys: list[str],
                  tier: str = "light", temperature: float = 0.2, timeout_sec: int = 60) -> dict:
        base_url = (config.get("llm_base_url") or "").rstrip("/")
        api_key = config.get("llm_api_key") or ""
        model = config.get("llm_model_strong" if tier == "strong" else "llm_model_light") or "gpt-4o-mini"
        if not base_url or not api_key:
            raise LLMError("LLM 网关未配置")

        url = base_url + "/chat/completions"
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        payload = {"model": model, "messages": messages, "temperature": temperature,
                   "response_format": {"type": "json_object"}}

        started = time.monotonic()
        last_err = ""
        for attempt in range(2):
            try:
                with httpx.Client(timeout=timeout_sec) as cli:
                    resp = cli.post(url, headers=headers, json=payload)
                if resp.status_code == 400 and "response_format" in payload:
                    payload.pop("response_format", None)  # 网关不支持则降级重发
                    continue
                if resp.status_code >= 400:
                    raise LLMError(f"LLM 网关返回 {resp.status_code}：{resp.text[:200]}")
                data = resp.json()
                content = data["choices"][0]["message"]["content"]
                usage = data.get("usage", {})
                obj = _extract_json(content)
                if obj is None or not all(k in obj for k in required_keys):
                    # 追加纠正消息重试 1 次
                    if attempt == 0:
                        messages = messages + [
                            {"role": "assistant", "content": content},
                            {"role": "user", "content": "你上一次的输出不是合法 JSON 或缺少必需字段。"
                                                        "请只输出符合要求格式的 JSON，不要包含任何解释、markdown 代码块或多余文字。"},
                        ]
                        continue
                    raise LLMFormatError("LLM 输出无法解析为要求的 JSON")
                self._log(scene, True, usage, started)
                return obj
            except (httpx.HTTPError, KeyError, json.JSONDecodeError) as e:
                last_err = str(e)
        self._log(scene, False, {}, started, error=last_err)
        raise LLMError(f"LLM 调用失败：{last_err}")

    def _log(self, scene: str, success: bool, usage: dict, started: float, error: str = "") -> None:
        try:
            with get_conn() as conn:
                conn.execute(
                    "INSERT INTO action_log(api_kind, endpoint, tokens_in, tokens_out, duration_ms, success, error, created_at) "
                    "VALUES ('llm', ?, ?, ?, ?, ?, ?, ?)",
                    (f"llm.{scene}", usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0),
                     int((time.monotonic() - started) * 1000), 1 if success else 0, error or None, utcnow_iso()),
                )
                conn.commit()
        except Exception:
            pass

    def ping(self) -> str:
        self.chat_json("ping", [{"role": "user", "content": "只回复 JSON：{\"ok\":true}"}],
                       required_keys=["ok"], tier="light", timeout_sec=20)
        return "连接成功"

    # ---------------- 启发式兜底（离线可测） ----------------
    def score_relevance_heuristic(self, tweets: list[dict]) -> list[dict]:
        results = []
        for t in tweets:
            text = (t.get("text") or "").lower()
            score = 4
            reason = "没命中任何正面/负面关键词，按中等偏低给分"
            if any(h.lower() in text for h in _NEGATIVE_HINTS):
                score, reason = 2, "命中新闻/招聘/营销/教程类关键词，疑似非本人诉求"
            elif any(h.lower() in text for h in _POSITIVE_HINTS):
                score, reason = 8, "命中成本/替代方案类关键词，疑似作者本人在表达痛点"
            results.append({"tweet_id": t["tweet_id"], "score": score,
                            "reason": "未配置 LLM，关键词粗略打分：" + reason})
        return results

    def match_heuristic(self, tweet_text: str, tweet_lang: str, candidates: list[dict]) -> dict:
        # 选与推文同语言的第一条候选；没有则跳过（真实 LLM 会做语义匹配）
        same_lang = [c for c in candidates if c.get("lang") == tweet_lang]
        pick = (same_lang or candidates)
        if not pick:
            return {"skip": True, "material_id": None, "reply_text": "", "confidence": 0.0,
                    "reason": "没有可用的回复素材"}
        chosen = pick[0]
        return {"skip": False, "material_id": chosen["material_id"], "reply_text": chosen["text"],
                "confidence": 0.72, "reason": "启发式：选用同语言候选素材（配置 LLM 后将做语义择优）"}

    # ---------------- 对上层的统一入口 ----------------
    def score_relevance(self, semantic_criteria: str, tweets: list[dict]) -> list[dict]:
        if not self.configured:
            return self.score_relevance_heuristic(tweets)
        messages = [
            {"role": "system", "content": prompts.RELEVANCE_SYSTEM},
            {"role": "user", "content": prompts.relevance_user(semantic_criteria, tweets)},
        ]
        obj = self.chat_json("relevance", messages, required_keys=["results"], tier="light", temperature=0.2)
        return obj["results"]

    def match_reply(self, tweet_text: str, tweet_lang: str, candidates: list[dict]) -> dict:
        if not self.configured:
            return self.match_heuristic(tweet_text, tweet_lang, candidates)
        messages = [
            {"role": "system", "content": prompts.MATCH_SYSTEM},
            {"role": "user", "content": prompts.match_user(tweet_text, tweet_lang, candidates)},
        ]
        return self.chat_json("match", messages,
                              required_keys=["skip", "reply_text", "confidence", "reason"],
                              tier="strong", temperature=0.7)


def _extract_json(text: str) -> dict | None:
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
