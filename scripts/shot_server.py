"""带样例数据起一个服务（端口 8099），供 shot_pages.py 截图用。用法见 README「测试」。"""
import os
import tempfile
from pathlib import Path

os.environ["X_OPERATOR_MOCK"] = "1"
from nicegui import ui  # noqa: E402

from x_operator.core import media  # noqa: E402
from x_operator.core.scheduler import Jobs  # noqa: E402
from x_operator.db.database import get_conn, init_db, utcnow_iso  # noqa: E402
from x_operator.ui import materials, queue, rules, schedule, settings_page, targets, watched, dashboard  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="xop_shot_"))
init_db(TMP / "t.db")
with get_conn() as conn:
    conn.execute("INSERT INTO accounts(handle, display_name, access_type, is_primary, credentials) VALUES ('main_acc','','official',1,'{}')")
    conn.execute("INSERT INTO accounts(handle, display_name, access_type, is_primary, is_premium, credentials) VALUES ('small1','','unofficial',0,1,'{\"auth_token\": \"a\"}')")
    conn.execute("INSERT INTO search_rules(name, keyword_query, semantic_criteria, lang, min_views, reply_mode) VALUES ('日本开发者', 'api', '抱怨太贵', 'ja', 1000, 'ai_write')")
    rel = media.new_rel_path("a.png")
    media.abs_path(rel).parent.mkdir(parents=True, exist_ok=True)
    media.abs_path(rel).write_bytes(bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d4944415478da63f8cfc0f01f0005000201cc4e2e0f0000000049454e44ae426082"))
    conn.execute("INSERT INTO materials(kind,text,lang,status,media_files,scenario_tags) VALUES ('reply','素材 A','ja','active',?, 'cost,recommend')", (media.dump_files([rel]),))
    conn.execute("INSERT INTO materials(kind,text,lang,status,created_by) VALUES ('post','素材 B','en','draft','ai')")
    for i, (st, sc, views) in enumerate([("queued", 8, 15000), ("no_match", 7, 300), ("filtered", 3, 50), ("new", None, 900)]):
        conn.execute("INSERT INTO target_tweets(tweet_id, author_id, author_handle, text, lang, view_count, tweet_created_at, source, source_rule_id, llm_relevance_score, process_status) "
                     "VALUES (?, '9', 'someone', ?, 'ja', ?, ?, 'search', 1, ?, ?)", (str(100 + i), f"サンプル推文 {i}", views, utcnow_iso(), sc, st))
    conn.execute("INSERT INTO review_queue(account_id, action_type, target_tweet_id, material_id, final_text, final_media_files, origin, is_auto_translated, status, created_at) "
                 "VALUES (1,'reply',1,1,'reply text https://example.com',?, 'manual', 1, 'pending',?)", (media.dump_files([rel]), utcnow_iso()))
    conn.execute("INSERT INTO review_queue(account_id, action_type, final_text, origin, status, created_at) VALUES (2,'post','post text', 'ai_write', 'pending',?)", (utcnow_iso(),))
    conn.commit()
jobs = Jobs()
for mod in (dashboard, materials, queue, rules, schedule, settings_page, targets, watched):
    mod.register(jobs)
ui.run(host="127.0.0.1", port=8099, show=False, reload=False, title="shot")
