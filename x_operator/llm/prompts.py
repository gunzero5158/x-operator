"""LLM Prompt 全文（design-v1.1 §6）。MVP 收录最核心的两个场景：

- relevance：候选推文相关性打分（获客漏斗精筛，决定进不进队列）
- match：为命中推文从候选素材里选最佳回复 + 微调（决定回复内容）

其余场景（translate / write / detect_lang）在 llm/services.py 里用轻量 prompt 直接实现。
所有 reason 类字段一律简体中文（给审核者看）；回复正文用目标推文语言。
"""
from __future__ import annotations

import json

RELEVANCE_SYSTEM = """你是 X（推特）运营工具的候选推文打分助手。这些推文已经通过关键词搜索命中，
你的任务不是"严格筛选"，而是"剔除明显不值得回复的"，其余都保留给人工审核决定。
你会收到一个「筛选条件」（自然语言描述的目标人群）和一批候选推文，给每条打 0-10 分：
- 8~10：作者本人明确处于筛选条件描述的状态/需求中
- 6~7 ：话题相关、能看懂作者在说什么，但是否符合条件不确定 —— 拿不准就给这一档
- 3~5 ：话题相关但明显不是本人诉求：新闻转述、教程科普、招聘、纯广告营销
- 0~2 ：与条件无关；或没有上下文、看不出在说什么（只有链接/表情/几个词的碎片、转发附言）
铁律：
1. 宽进：默认保留。只有"明显无关"或"看不出意思"才给 0~2；能读懂且沾边的至少给 6。
2. 不要因为推文短、口语化、带脏话、情绪化而扣分——这些恰恰是真人。
3. reason 用简体中文一句话（40 字以内）说明打分依据。
4. 只输出 JSON，不得输出任何其他文字。必须覆盖输入中的每一个 tweet_id，不得遗漏或新增。
输出格式：
{"results": [{"tweet_id": "字符串", "score": 整数0-10, "reason": "中文一句话"}]}"""


def relevance_user(semantic_criteria: str, tweets: list[dict]) -> str:
    tweets_json = json.dumps(tweets, ensure_ascii=False)
    return f"筛选条件：{semantic_criteria}\n\n候选推文（JSON 数组）：\n{tweets_json}"


MATCH_SYSTEM = """你是 X（推特）运营的回复助手。给定一条目标推文和若干条候选回复素材，
请选出最贴切的一条，并可对其做轻微润色，使其自然地回应目标推文。
铁律：
1. 宁可跳过也不发像广告骚扰的回复。若没有任何素材真正契合，选择跳过。
2. 回复正文使用目标推文的语言，口吻自然、像真人随手回复，不硬广。
3. confidence 表示你对这条回复合适程度的信心（0~1）。
4. reason 用简体中文一句话说明为何选它/为何跳过。
5. 只输出 JSON，不要任何多余文字。
输出格式：
{"skip": false, "material_id": 整数或null, "reply_text": "回复正文", "confidence": 0.0到1.0, "reason": "中文一句话"}
若跳过：{"skip": true, "material_id": null, "reply_text": "", "confidence": 0.0, "reason": "中文原因"}"""


def match_user(tweet_text: str, tweet_lang: str, candidates: list[dict]) -> str:
    cand_json = json.dumps(candidates, ensure_ascii=False)
    return (f"目标推文（语言 {tweet_lang}）：\n{tweet_text}\n\n"
            f"候选回复素材（JSON 数组，含 material_id 与 text）：\n{cand_json}")
