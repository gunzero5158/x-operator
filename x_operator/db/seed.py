"""首启动种子：app_settings 默认值（对齐 design-v1.1 §10.1）+ 一批演示数据。

演示数据让用户 clone 后不配置任何东西也能立刻在 UI 里跑通「监控→打分→匹配→
审核→（模拟）发送」全流程。真实使用时可在设置页清空或忽略。
"""
from __future__ import annotations

import sqlite3

# 键 → 默认值（全部字符串存储；读取侧按需转型）。
DEFAULT_SETTINGS: dict[str, str] = {
    # 运行模式：dry_run=1 时所有发送走 Mock 适配器，不碰真实 X（MVP 默认）
    "dry_run": "1",
    # LLM（OpenAI 兼容网关；留空则用启发式兜底打分，离线可测）
    "llm_base_url": "",
    "llm_api_key": "",
    "llm_model_light": "gpt-4o-mini",
    "llm_model_strong": "gpt-4o",
    # 合规参数（design-v1.1 §10.1 defaults）
    "cooldown_days": "7",
    "grace_period_hours": "2",
    "reply_ttl_hours": "48",
    "tweet_max_age_hours": "48",
    "nurture_days": "14",
    "match_confidence_threshold": "0.7",
    # 预算
    "billing_mode": "payg",
    "monthly_budget_usd": "60",
    "monthly_read_quota": "10000",
    "daily_read_budget": "330",
    "budget_reserve_reads": "20",
    "monitor_interval_minutes": "50",
    "search_runs_per_day": "2",
    # 调度开关（MVP：默认关闭自动轮询，改用 UI 上的「立即运行」按钮手动触发，
    # 便于测试期精确控制；设置页可打开）
    "auto_jobs_enabled": "0",
}


def seed_settings(conn: sqlite3.Connection) -> None:
    for key, value in DEFAULT_SETTINGS.items():
        conn.execute(
            "INSERT INTO app_settings(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO NOTHING",
            (key, value),
        )


def seed_demo_data(conn: sqlite3.Connection) -> None:
    # 一个演示用官方账号（Mock 模式下无需真实凭据）
    conn.execute(
        "INSERT INTO accounts(handle, display_name, access_type, is_primary, credential_ref, "
        "daily_post_limit, daily_reply_limit, min_interval_sec, max_interval_sec, note) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("apimax_jp", "ApiMax Japan", "official", 1, "apimax_jp",
         10, 15, 60, 180, "演示账号（Mock 模式，无需真实凭据）"),
    )

    # 回复素材（reply）——日/英/中三语，用于匹配引擎演示
    reply_materials = [
        ("reply", "画像生成のAPIコストで悩んでいるなら、従量課金で複数モデルをまとめて使える選択肢もありますよ。よければ詳細シェアします🙌",
         "ja", "ai,api,cost"),
        ("reply", "If API pricing is the blocker, there are pay-as-you-go gateways that bundle multiple models under one bill. Happy to share what worked for us.",
         "en", "ai,api,cost"),
        ("reply", "如果是卡在 API 成本上，其实有按量计费、多模型统一结算的方案，需要的话可以分享下我们的经验～",
         "zh", "ai,api,cost"),
        ("reply", "モデル選定は用途次第ですが、複数モデルを一つの窓口で試せると比較が早いです。参考までに。",
         "ja", "ai,model,compare"),
    ]
    # 让前三条组成同一翻译组
    cur = conn.cursor()
    group_id = None
    for i, (kind, text, lang, tags) in enumerate(reply_materials):
        cur.execute(
            "INSERT INTO materials(kind, text, lang, scenario_tags, status, created_by) "
            "VALUES (?,?,?,?,'active','human')",
            (kind, text, lang, tags),
        )
        mid = cur.lastrowid
        if i == 0:
            group_id = mid
        if i < 3:
            cur.execute("UPDATE materials SET translation_group_id=? WHERE id=?", (group_id, mid))

    # 发帖素材（post）——用于定时发布演示
    conn.execute(
        "INSERT INTO materials(kind, text, lang, scenario_tags, status, created_by) "
        "VALUES ('post', ?, 'ja', 'promo', 'active', 'human')",
        ("複数のLLMを一つのAPIキーで。従量課金で無駄なく使えます。#AI #API",),
    )

    # 监控推主（演示用，x_user_id 为 Mock 生成的稳定假 id）
    conn.execute(
        "INSERT INTO watched_users(handle, x_user_id, include_replies, enabled, note) "
        "VALUES (?,?,0,1,?)",
        ("indie_ai_dev", "mock_user_indie_ai_dev", "演示：独立 AI 开发者"),
    )

    # 搜索规则（演示：找正在为 AI API 成本发愁的人）
    conn.execute(
        "INSERT INTO search_rules(name, keyword_query, semantic_criteria, lang, min_llm_score, max_results_per_run) "
        "VALUES (?,?,?,?,?,?)",
        ("AI API 成本痛点",
         "(API 料金 OR API コスト OR API高い) (AI OR LLM) -is:retweet lang:ja",
         "作者本人正在为 AI/LLM 的 API 调用成本发愁，或在寻找更便宜的替代方案。排除新闻、教程、招聘、营销推广。",
         "ja", 7, 15),
    )
