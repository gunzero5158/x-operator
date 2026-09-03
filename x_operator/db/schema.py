"""数据库 DDL（SQLite 方言），与 docs/design-v1.1.md §4 一一对应。

MVP 说明：本文件即当前 schema 版本的建表 SQL（旧库由 database._migrate 增量补列）。
v2 新增：accounts.credentials（账号凭据 JSON）、materials.deleted_at（素材回收站软删除）。为减少依赖、便于测试期快速改表，
这里用原生 sqlite3 而非 SQLAlchemy。表结构与字段名严格对齐 spec，方便将来长成完整版。
"""

SCHEMA_VERSION = 6

DDL = r"""
CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    handle              TEXT    NOT NULL UNIQUE,
    display_name        TEXT    NOT NULL DEFAULT '',
    access_type         TEXT    NOT NULL CHECK (access_type IN ('official','unofficial')),
    is_primary          INTEGER NOT NULL DEFAULT 0 CHECK (is_primary IN (0,1)),
    credential_ref      TEXT    NOT NULL DEFAULT '',
    credentials         TEXT    NOT NULL DEFAULT '{}',
    daily_post_limit    INTEGER NOT NULL DEFAULT 10  CHECK (daily_post_limit  >= 0),
    daily_reply_limit   INTEGER NOT NULL DEFAULT 15  CHECK (daily_reply_limit >= 0),
    min_interval_sec    INTEGER NOT NULL DEFAULT 180 CHECK (min_interval_sec >= 0),
    max_interval_sec    INTEGER NOT NULL DEFAULT 600 CHECK (max_interval_sec >= min_interval_sec),
    active_hours_start  TEXT    NOT NULL DEFAULT '09:00',
    active_hours_end    TEXT    NOT NULL DEFAULT '22:00',
    timezone            TEXT    NOT NULL DEFAULT 'Asia/Tokyo',
    status              TEXT    NOT NULL DEFAULT 'active'
                        CHECK (status IN ('active','paused','auth_error')),
    next_allowed_at     TEXT,
    note                TEXT    NOT NULL DEFAULT '',
    created_at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    CHECK (NOT (is_primary = 1 AND access_type = 'unofficial'))
);

CREATE TABLE IF NOT EXISTS materials (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    kind                 TEXT    NOT NULL CHECK (kind IN ('post','reply')),
    text                 TEXT    NOT NULL,
    lang                 TEXT    NOT NULL,
    translation_group_id INTEGER,
    scenario_tags        TEXT    NOT NULL DEFAULT '',
    media_ids            TEXT    NOT NULL DEFAULT '[]',
    created_by           TEXT    NOT NULL DEFAULT 'human' CHECK (created_by IN ('human','ai')),
    status               TEXT    NOT NULL DEFAULT 'draft'
                         CHECK (status IN ('draft','active','archived')),
    usage_count          INTEGER NOT NULL DEFAULT 0,
    last_used_at         TEXT,
    deleted_at           TEXT,
    created_at           TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE INDEX IF NOT EXISTS ix_materials_pick  ON materials(kind, status, lang);
CREATE INDEX IF NOT EXISTS ix_materials_group ON materials(translation_group_id);

CREATE TABLE IF NOT EXISTS scheduled_posts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id     INTEGER NOT NULL REFERENCES accounts(id),
    material_id    INTEGER NOT NULL REFERENCES materials(id),
    schedule_type  TEXT    NOT NULL CHECK (schedule_type IN ('once','daily','weekly','cron')),
    schedule_expr  TEXT    NOT NULL,
    next_run_at    TEXT,
    auto_approve   INTEGER NOT NULL DEFAULT 0 CHECK (auto_approve IN (0,1)),
    status         TEXT    NOT NULL DEFAULT 'active'
                   CHECK (status IN ('active','paused','done','missed')),
    last_run_at    TEXT,
    created_at     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE INDEX IF NOT EXISTS ix_sched_due ON scheduled_posts(status, next_run_at);

CREATE TABLE IF NOT EXISTS watched_users (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    handle             TEXT    NOT NULL UNIQUE,
    x_user_id          TEXT    NOT NULL UNIQUE,
    last_seen_tweet_id TEXT,
    include_replies    INTEGER NOT NULL DEFAULT 0 CHECK (include_replies IN (0,1)),
    enabled            INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1)),
    hit_count          INTEGER NOT NULL DEFAULT 0,
    lookback_hours     INTEGER NOT NULL DEFAULT 24,
    reply_mode         TEXT    NOT NULL DEFAULT 'material',
    ai_brief           TEXT    NOT NULL DEFAULT '',
    allow_polish       INTEGER NOT NULL DEFAULT 0,
    note               TEXT    NOT NULL DEFAULT '',
    created_at         TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE IF NOT EXISTS search_rules (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    name                TEXT    NOT NULL UNIQUE,
    keyword_query       TEXT    NOT NULL,
    semantic_criteria   TEXT    NOT NULL,
    lang                TEXT    NOT NULL DEFAULT 'ja',
    newest_id_cursor    TEXT,
    max_results_per_run INTEGER NOT NULL DEFAULT 15 CHECK (max_results_per_run BETWEEN 10 AND 100),
    min_llm_score       INTEGER NOT NULL DEFAULT 5  CHECK (min_llm_score BETWEEN 0 AND 10),
    lookback_hours      INTEGER NOT NULL DEFAULT 24,
    min_views           INTEGER NOT NULL DEFAULT 0,
    reply_mode          TEXT    NOT NULL DEFAULT 'material',
    ai_brief            TEXT    NOT NULL DEFAULT '',
    allow_polish        INTEGER NOT NULL DEFAULT 0,
    enabled             INTEGER NOT NULL DEFAULT 1  CHECK (enabled IN (0,1)),
    last_run_at         TEXT,
    created_at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE IF NOT EXISTS target_tweets (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    tweet_id             TEXT    NOT NULL UNIQUE,
    author_id            TEXT    NOT NULL,
    author_handle        TEXT    NOT NULL DEFAULT '',
    text                 TEXT    NOT NULL,
    lang                 TEXT,
    text_zh              TEXT,
    view_count           INTEGER,
    tweet_created_at     TEXT    NOT NULL,
    source               TEXT    NOT NULL CHECK (source IN ('monitor','search')),
    source_rule_id       INTEGER,
    llm_relevance_score  INTEGER CHECK (llm_relevance_score BETWEEN 0 AND 10),
    llm_relevance_reason TEXT,
    process_status       TEXT    NOT NULL DEFAULT 'new'
                         CHECK (process_status IN ('new','queued','no_match','filtered','expired')),
    fetched_at           TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE INDEX IF NOT EXISTS ix_target_status ON target_tweets(process_status, fetched_at);
CREATE INDEX IF NOT EXISTS ix_target_author ON target_tweets(author_id);

CREATE TABLE IF NOT EXISTS review_queue (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id        INTEGER NOT NULL REFERENCES accounts(id),
    action_type       TEXT    NOT NULL CHECK (action_type IN ('post','reply')),
    target_tweet_id   INTEGER REFERENCES target_tweets(id),
    material_id       INTEGER REFERENCES materials(id),
    scheduled_post_id INTEGER REFERENCES scheduled_posts(id),
    final_text        TEXT    NOT NULL,
    final_media_ids   TEXT    NOT NULL DEFAULT '[]',
    llm_reason        TEXT    NOT NULL DEFAULT '',
    llm_confidence    REAL    CHECK (llm_confidence BETWEEN 0 AND 1),
    is_auto_translated INTEGER NOT NULL DEFAULT 0 CHECK (is_auto_translated IN (0,1)),
    status            TEXT    NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending','approved','sending','sent',
                                        'failed','skipped','expired')),
    skip_reason       TEXT,
    auto_approve      INTEGER NOT NULL DEFAULT 0 CHECK (auto_approve IN (0,1)),
    retry_count       INTEGER NOT NULL DEFAULT 0,
    sent_tweet_id     TEXT,
    error_msg         TEXT,
    origin            TEXT    NOT NULL DEFAULT 'ai_match',
    verify_status     TEXT,
    created_at        TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    decided_at        TEXT,
    sent_at           TEXT,
    expires_at        TEXT,
    CHECK (NOT (action_type = 'reply' AND target_tweet_id IS NULL))
);
CREATE INDEX IF NOT EXISTS ix_queue_dispatch ON review_queue(status, account_id, created_at);
CREATE INDEX IF NOT EXISTS ix_queue_pending  ON review_queue(status, expires_at);

CREATE TABLE IF NOT EXISTS interactions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id  INTEGER NOT NULL REFERENCES accounts(id),
    action      TEXT    NOT NULL CHECK (action IN ('post','reply')),
    tweet_id    TEXT    NOT NULL,
    author_id   TEXT,
    sent_at     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_interactions_reply ON interactions(tweet_id) WHERE action = 'reply';
CREATE INDEX IF NOT EXISTS ix_interactions_cooldown ON interactions(author_id, sent_at);
CREATE INDEX IF NOT EXISTS ix_interactions_daily    ON interactions(account_id, action, sent_at);

CREATE TABLE IF NOT EXISTS blacklist (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    x_user_id  TEXT    NOT NULL UNIQUE,
    handle     TEXT    NOT NULL DEFAULT '',
    reason     TEXT    NOT NULL DEFAULT '',
    created_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE IF NOT EXISTS action_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id     INTEGER,
    api_kind       TEXT    NOT NULL CHECK (api_kind IN ('x_official','x_unofficial','x_mock','llm')),
    endpoint       TEXT    NOT NULL,
    reads_consumed INTEGER NOT NULL DEFAULT 0,
    tokens_in      INTEGER NOT NULL DEFAULT 0,
    tokens_out     INTEGER NOT NULL DEFAULT 0,
    duration_ms    INTEGER NOT NULL DEFAULT 0,
    has_link       INTEGER NOT NULL DEFAULT 0,
    success        INTEGER NOT NULL CHECK (success IN (0,1)),
    error          TEXT,
    created_at     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE INDEX IF NOT EXISTS ix_actionlog_usage ON action_log(api_kind, created_at);

CREATE TABLE IF NOT EXISTS app_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""
