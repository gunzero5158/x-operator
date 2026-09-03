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


# ====================================================================================
# 场景 → 用哪档模型。这是**唯一**的登记表：chat_json 只认这里登记过的场景，没登记的直接报错，
# 所以以后加任何新的 LLM 功能都必须先在这里补一行（设置 → LLM 页和 README 的对照表都从这里渲染）。
# 原则：量大、判断简单的用轻量模型；要写东西、要做取舍的用强模型。
# ====================================================================================
SCENE_TIERS: dict[str, tuple[str, str]] = {
    "ping":         ("light",  "设置页「保存并测试连接」"),
    "relevance":    ("light",  "搜索规则：给每条抓到的推文打相关性分（量大、判断简单）"),
    "match":        ("strong", "匹配素材库：从候选素材里择优；开了「允许润色」时还要改写"),
    "write":        ("strong", "AI 按要求创作回复"),
    "rule_gen":     ("strong", "AI 生成搜索规则（关键词、语义条件、语言）"),
    "material_gen": ("strong", "AI 生成素材"),
}
TIER_LABEL = {"light": "轻量模型", "strong": "强模型"}
TIER_SETTING_KEY = {"light": "llm_model_light", "strong": "llm_model_strong"}
TIER_DEFAULT_MODEL = {"light": "gpt-4o-mini", "strong": "gpt-4o"}


def model_for(scene: str) -> str:
    """按登记表取该场景当前应使用的模型名。未登记的场景抛 LLMError。"""
    if scene not in SCENE_TIERS:
        raise LLMError(f"LLM 场景「{scene}」没有在 SCENE_TIERS 登记（x_operator/llm/client.py），"
                       "请先登记它该用轻量模型还是强模型")
    tier = SCENE_TIERS[scene][0]
    return config.get(TIER_SETTING_KEY[tier]) or TIER_DEFAULT_MODEL[tier]


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
                  temperature: float = 0.2, timeout_sec: int = 60) -> dict:
        """scene 必须是 SCENE_TIERS 里登记过的场景名，用哪个模型由登记表决定。"""
        model = model_for(scene)
        base_url = (config.get("llm_base_url") or "").rstrip("/")
        api_key = config.get("llm_api_key") or ""
        if not base_url or not api_key:
            raise LLMError("LLM 网关未配置")

        url = base_url + "/chat/completions"
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        payload = {"model": model, "messages": list(messages), "temperature": temperature,
                   "response_format": {"type": "json_object"}}

        started = time.monotonic()
        last_err = ""
        corrected = False      # 已追加过一次「请只输出 JSON」的纠正
        downgraded = False     # 已去掉 response_format 重发过
        for _ in range(4):     # 最多：首发 + 降级重发 + 纠正重发 + 一次网络重试
            try:
                with httpx.Client(timeout=timeout_sec) as cli:
                    resp = cli.post(url, headers=headers, json=payload)
                if resp.status_code == 400 and "response_format" in payload and not downgraded:
                    payload.pop("response_format", None)  # 网关不支持则降级重发
                    downgraded = True
                    continue
                if resp.status_code >= 400:
                    last_err = f"LLM 网关返回 {resp.status_code}：{resp.text[:200]}"
                    break
                data = resp.json()
                content = data["choices"][0]["message"]["content"]
                usage = data.get("usage", {}) or {}
                obj = _extract_json(content)
                if obj is None or not all(k in obj for k in required_keys):
                    if not corrected:
                        # 把模型的回答和纠正要求一起发回去（必须写进 payload，否则重发的还是原话）
                        payload["messages"] = list(payload["messages"]) + [
                            {"role": "assistant", "content": str(content)},
                            {"role": "user", "content": "你上一次的输出不是合法 JSON 或缺少必需字段。"
                                                        "请只输出符合要求格式的 JSON，不要包含任何解释、markdown 代码块或多余文字。"},
                        ]
                        corrected = True
                        continue
                    last_err = "LLM 输出无法解析为要求的 JSON"
                    self._log(scene, False, usage, started, error=last_err)
                    raise LLMFormatError(last_err)
                self._log(scene, True, usage, started)
                return obj
            except (httpx.HTTPError, KeyError, IndexError, TypeError, json.JSONDecodeError) as e:
                last_err = f"{type(e).__name__}: {e}"
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
                       required_keys=["ok"], timeout_sec=20)
        return "连接成功"

    # ---------------- 启发式兜底（离线可测） ----------------
    def score_relevance_heuristic(self, tweets: list[dict]) -> list[dict]:
        """无 LLM 时的粗打分。思路是「宽进」：推文已经被关键词搜索命中，默认 7 分保留；
        只有明显是新闻/招聘/广告（3 分）或没有上下文、看不出意思（2 分）才压到达标线下。"""
        results = []
        for t in tweets:
            raw = t.get("text") or ""
            text = raw.lower()
            core = _strip_noise(raw)
            if len(core) < 12:
                score, reason = 2, f"去掉链接/@/#/表情后只剩 {len(core)} 个字，看不出上下文"
            elif any(h.lower() in text for h in _NEGATIVE_HINTS):
                score, reason = 3, "命中新闻/招聘/营销/教程类关键词，疑似非本人诉求"
            elif any(h.lower() in text for h in _POSITIVE_HINTS):
                score, reason = 8, "命中成本/替代方案类关键词，疑似作者本人在表达痛点"
            else:
                score, reason = 7, "关键词已命中且有完整上下文，默认保留，交给人工审核判断"
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
        obj = self.chat_json("relevance", messages, required_keys=["results"], temperature=0.2)
        return obj["results"]

    def match_reply(self, tweet_text: str, tweet_lang: str, candidates: list[dict],
                    allow_polish: bool = False) -> dict:
        if not self.configured:
            return self.match_heuristic(tweet_text, tweet_lang, candidates)
        messages = [
            {"role": "system", "content": prompts.match_system(allow_polish)},
            {"role": "user", "content": prompts.match_user(tweet_text, tweet_lang, candidates)},
        ]
        return self.chat_json("match", messages,
                              required_keys=["skip", "reply_text", "confidence", "reason"],
                              temperature=0.7 if allow_polish else 0.2)

    # ---------------- 需要真实 LLM 的创作类能力（无网关时抛 LLMError，UI 会提示去配置） ----------------
    def _require(self, what: str) -> None:
        if not self.configured:
            raise LLMError(f"{what}需要 LLM：请先到「设置 → LLM」填好网关 base_url 和 api_key")

    def write_reply(self, tweet_text: str, tweet_lang: str, brief: str, must_include: list[str]) -> dict:
        """按创作要求为一条推文写回复。返回 {reply_text, reason}；必须包含项缺失会重试一次。"""
        self._require("AI 撰写回复")
        messages = [
            {"role": "system", "content": prompts.WRITE_SYSTEM},
            {"role": "user", "content": prompts.write_user(tweet_text, tweet_lang, brief, must_include)},
        ]
        for attempt in range(2):
            obj = self.chat_json("write", messages, required_keys=["reply_text", "reason"], temperature=0.8)
            text = str(obj.get("reply_text") or "")
            missing = [m for m in must_include if m and m not in text]
            if not missing:
                return {"reply_text": text, "reason": str(obj.get("reason") or "")}
            messages = messages + [
                {"role": "assistant", "content": json.dumps(obj, ensure_ascii=False)},
                {"role": "user", "content": "你的回复缺少了必须原样包含的字符串：" + "、".join(missing) + "。请重写，务必包含。"},
            ]
        raise LLMFormatError("AI 两次都没把必须包含的内容写进去：" + "、".join(missing))

    def generate_search_rule(self, description: str) -> dict:
        """自然语言描述 → {name, keywords[], semantic_criteria, langs[]}。"""
        self._require("AI 生成搜索规则")
        messages = [
            {"role": "system", "content": prompts.RULE_GEN_SYSTEM},
            {"role": "user", "content": prompts.rule_gen_user(description)},
        ]
        obj = self.chat_json("rule_gen", messages, required_keys=["name", "keywords", "semantic_criteria", "langs"],
                             temperature=0.5)
        obj["keywords"] = [str(k).strip() for k in (obj.get("keywords") or []) if str(k).strip()]
        obj["langs"] = [str(x).strip().lower() for x in (obj.get("langs") or []) if str(x).strip()]
        return obj

    def generate_materials(self, kind: str, lang: str, topic: str, style: str, scenario: str,
                           must_include: list[str], count: int) -> list[dict]:
        """批量生成素材 → [{text, scenario_tags}]。"""
        self._require("AI 生成素材")
        messages = [
            {"role": "system", "content": prompts.MATERIAL_GEN_SYSTEM},
            {"role": "user", "content": prompts.material_gen_user(kind, lang, topic, style, scenario, must_include, count)},
        ]
        obj = self.chat_json("material_gen", messages, required_keys=["items"], temperature=0.9, timeout_sec=120)
        items = []
        for it in obj.get("items") or []:
            text = str(it.get("text") or "").strip()
            if text:
                items.append({"text": text, "scenario_tags": str(it.get("scenario_tags") or "").strip()})
        return items


_NOISE_RE = re.compile(r"https?://\S+|www\.\S+|[@#]\S+|\s+")
_EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF☀-➿️]")


def _strip_noise(text: str) -> str:
    """去掉链接、@、#话题、表情、空白后剩下的「正文」，用来判断有没有上下文。"""
    return _EMOJI_RE.sub("", _NOISE_RE.sub("", text or ""))


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
