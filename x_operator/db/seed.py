"""首启动种子：只写 app_settings 默认值（对齐 design-v1.1 §10.1）。

不再写任何演示/Mock 数据：账号、素材、监控推主、搜索规则全部由用户在 UI 里自己添加。
"""
from __future__ import annotations

import sqlite3

# 键 → 默认值（全部字符串存储；读取侧按需转型）。
DEFAULT_SETTINGS: dict[str, str] = {
    # LLM（OpenAI 兼容网关；留空则用启发式兜底打分，离线可测）
    "llm_base_url": "",
    "llm_api_key": "",
    "llm_model_light": "gpt-4o-mini",
    "llm_model_strong": "gpt-4o",
    # 合规参数（design-v1.1 §10.1 defaults）
    "cooldown_days": "7",
    "grace_period_hours": "2",
    "reply_ttl_hours": "48",
    "nurture_days": "14",
    "match_confidence_threshold": "0.7",
    # 预算（读额度由 core/budget.py 实际执行：自动轮询在触及熔断线时停，手动运行在用完时拒绝）
    "monthly_budget_usd": "60",
    "daily_read_budget": "330",
    "budget_reserve_reads": "20",
    "monitor_interval_minutes": "50",
    "search_runs_per_day": "2",
    # 调度开关（默认关闭自动轮询，用 UI 上的「运行一次」按钮手动触发；设置页可打开）
    "auto_jobs_enabled": "0",
}

# 已废弃、任何代码都不再读取的设置键：每次启动顺手删掉，免得设置页/导出里误导人
OBSOLETE_SETTINGS = ("dry_run", "tweet_max_age_hours", "billing_mode", "monthly_read_quota")


def seed_settings(conn: sqlite3.Connection, overrides: dict | None = None) -> None:
    values = dict(DEFAULT_SETTINGS)
    for key, val in (overrides or {}).items():
        if key in values:
            values[key] = "1" if val is True else ("0" if val is False else str(val))
    for key, value in values.items():
        conn.execute(
            "INSERT INTO app_settings(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO NOTHING",
            (key, value),
        )
    marks = ",".join("?" * len(OBSOLETE_SETTINGS))
    conn.execute(f"DELETE FROM app_settings WHERE key IN ({marks})", OBSOLETE_SETTINGS)


# ---- 历史版本（≤ v2）首启动时写入过的演示数据：升级到 v3 时按这些特征精确清除 ----
DEMO_ACCOUNT_HANDLE = "apimax_jp"
DEMO_MATERIAL_TEXTS = (
    "画像生成のAPIコストで悩んでいるなら、従量課金で複数モデルをまとめて使える選択肢もありますよ。よければ詳細シェアします🙌",
    "If API pricing is the blocker, there are pay-as-you-go gateways that bundle multiple models under one bill. Happy to share what worked for us.",
    "如果是卡在 API 成本上，其实有按量计费、多模型统一结算的方案，需要的话可以分享下我们的经验～",
    "モデル選定は用途次第ですが、複数モデルを一つの窓口で試せると比較が早いです。参考までに。",
    "複数のLLMを一つのAPIキーで。従量課金で無駄なく使えます。#AI #API",
)
DEMO_RULE_QUERY = "(API 料金 OR API コスト OR API高い) (AI OR LLM) -is:retweet lang:ja"


def purge_demo_data(conn: sqlite3.Connection) -> dict[str, int]:
    """清除旧版本留下的全部演示数据。返回各类删除数量（供日志）。幂等。"""
    n: dict[str, int] = {}

    def run(label: str, sql: str, args: tuple = ()) -> None:
        n[label] = n.get(label, 0) + conn.execute(sql, args).rowcount

    # 1) Mock 适配器产生的抓取记录 / 队列 / 账本 / 日志
    run("review_queue", "DELETE FROM review_queue WHERE target_tweet_id IN "
                        "(SELECT id FROM target_tweets WHERE author_id LIKE 'mock_user_%')")
    run("review_queue", "DELETE FROM review_queue WHERE sent_tweet_id LIKE 'mock_%'")
    run("target_tweets", "DELETE FROM target_tweets WHERE author_id LIKE 'mock_user_%'")
    run("interactions", "DELETE FROM interactions WHERE author_id LIKE 'mock_user_%' OR tweet_id LIKE 'mock_%'")
    run("action_log", "DELETE FROM action_log WHERE api_kind='x_mock'")
    # 2) 演示监控推主 / 演示搜索规则
    run("watched_users", "DELETE FROM watched_users WHERE x_user_id LIKE 'mock_user_%'")
    run("search_rules", "DELETE FROM search_rules WHERE keyword_query=?", (DEMO_RULE_QUERY,))
    # 3) 演示账号（没填凭据的 apimax_jp）及其全部关联记录
    row = conn.execute("SELECT id FROM accounts WHERE handle=? AND (credentials IS NULL OR credentials='{}')",
                       (DEMO_ACCOUNT_HANDLE,)).fetchone()
    if row:
        aid = row["id"]
        run("review_queue", "DELETE FROM review_queue WHERE account_id=?", (aid,))
        run("scheduled_posts", "DELETE FROM scheduled_posts WHERE account_id=?", (aid,))
        run("interactions", "DELETE FROM interactions WHERE account_id=?", (aid,))
        run("action_log", "DELETE FROM action_log WHERE account_id=?", (aid,))
        run("accounts", "DELETE FROM accounts WHERE id=?", (aid,))
    # 4) 演示素材（正文完全一致且从未被用过的才删；被定时计划引用的跳过）
    marks = ",".join("?" * len(DEMO_MATERIAL_TEXTS))
    ids = [r["id"] for r in conn.execute(
        f"SELECT id FROM materials WHERE text IN ({marks}) AND usage_count=0 "
        "AND id NOT IN (SELECT material_id FROM scheduled_posts)", DEMO_MATERIAL_TEXTS).fetchall()]
    for mid in ids:
        conn.execute("UPDATE review_queue SET material_id=NULL WHERE material_id=?", (mid,))
        conn.execute("UPDATE materials SET translation_group_id=NULL WHERE translation_group_id=?", (mid,))
        run("materials", "DELETE FROM materials WHERE id=?", (mid,))
    # 5) 已废弃的设置项
    run("app_settings", "DELETE FROM app_settings WHERE key='dry_run'")
    return {k: v for k, v in n.items() if v}


def loosen_filters(conn: sqlite3.Connection) -> dict[str, int]:
    """v4：过滤策略从「宁缺毋滥」改为「默认保留」——把还停留在旧默认值上的阈值放宽。
    只动等于旧默认值的（说明用户没自己调过）：达标分 7/6 → 5。（全局「推文最大年龄」已在 v5 之后废弃，
    时间窗改为每条规则/推主自己的「首次回溯」。）"""
    n: dict[str, int] = {}
    n["search_rules"] = conn.execute("UPDATE search_rules SET min_llm_score=5 WHERE min_llm_score IN (6, 7)").rowcount
    return {k: v for k, v in n.items() if v}
