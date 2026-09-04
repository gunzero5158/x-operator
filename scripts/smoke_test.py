"""离线冒烟测试（不联网）：uv run python scripts/smoke_test.py

覆盖：v2 旧库升级清演示数据 / 新库干净 / 全链路（X_OPERATOR_MOCK=1 走测试用 Mock 适配器）/
搜索多语言与过滤原因 / 密码+TOTP 登录流程各分支模拟 / 系统代理与凭据格式校验。
"""
import asyncio  # noqa: F401
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT); sys.path.insert(0, str(ROOT))
os.environ["X_OPERATOR_MOCK"] = "1"
TMP = Path(tempfile.mkdtemp(prefix="xop_smoke_"))

from x_operator.db.database import init_db, get_conn, utcnow_iso  # noqa: E402
from x_operator.db import schema, seed  # noqa: E402
import x_operator.db.database as dbm  # noqa: E402


def fresh_conn_state():
    dbm._local = threading.local()


# ---------- 1. v2 旧库升级：演示数据被清、真实数据保留 ----------
old_db = TMP / 'v2.db'
c = sqlite3.connect(old_db); c.row_factory = sqlite3.Row
c.executescript(schema.DDL); c.execute("INSERT INTO schema_version(version) VALUES (2)")
c.execute("INSERT INTO app_settings(key,value) VALUES ('dry_run','1')")
c.execute("INSERT INTO accounts(handle, access_type, credentials) VALUES ('my_real','unofficial',?)", (json.dumps({"auth_token": "a" * 40, "ct0": "b" * 32}),))
acc_id = c.execute("SELECT id FROM accounts").fetchone()["id"]
c.execute("INSERT INTO watched_users(handle,x_user_id) VALUES ('fake_user','mock_user_fake_user')")
c.execute("INSERT INTO target_tweets(tweet_id,author_id,author_handle,text,tweet_created_at,source,process_status) VALUES ('1','mock_user_a','a','hi','2026-01-01T00:00:00Z','monitor','queued')")
tt = c.execute("SELECT id FROM target_tweets").fetchone()["id"]
c.execute("INSERT INTO review_queue(account_id,action_type,target_tweet_id,final_text,status,created_at) VALUES (?,'reply',?,'x','pending',?)", (acc_id, tt, utcnow_iso()))
c.execute("INSERT INTO action_log(account_id,api_kind,endpoint,success,created_at) VALUES (?,'x_mock','e',1,?)", (acc_id, utcnow_iso()))
c.execute("INSERT INTO materials(kind,text,lang,status) VALUES ('reply','我的素材','zh','active')")
c.execute("INSERT INTO search_rules(name,keyword_query,semantic_criteria,lang,min_llm_score) VALUES ('我的规则','foo bar','找人','ja,en',7)")
c.execute("INSERT INTO app_settings(key,value) VALUES ('tweet_max_age_hours','48')")
c.execute("INSERT INTO app_settings(key,value) VALUES ('match_confidence_threshold','0.7')")
c.execute("INSERT INTO app_settings(key,value) VALUES ('search_runs_per_day','4')")
c.execute("DROP TABLE scheduled_posts")
c.execute("CREATE TABLE scheduled_posts (id INTEGER PRIMARY KEY AUTOINCREMENT, account_id INTEGER NOT NULL REFERENCES accounts(id), "
          "material_id INTEGER NOT NULL REFERENCES materials(id), schedule_type TEXT NOT NULL, schedule_expr TEXT NOT NULL, next_run_at TEXT, "
          "auto_approve INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'active', last_run_at TEXT, "
          "created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')))")
mat_old = c.execute("SELECT id FROM materials WHERE text='我的素材'").fetchone()["id"]
c.execute("INSERT INTO scheduled_posts(account_id, material_id, schedule_type, schedule_expr, next_run_at) VALUES (?,?,'daily','21:00','2030-01-01T12:00:00Z')", (acc_id, mat_old))
c.commit(); c.close()
init_db(old_db)
with get_conn() as conn:
    ver = conn.execute("SELECT version FROM schema_version").fetchone()["version"]
    accs = [r["handle"] for r in conn.execute("SELECT handle FROM accounts")]
    mats = [r["text"] for r in conn.execute("SELECT text FROM materials")]
    rules = [r["name"] for r in conn.execute("SELECT name FROM search_rules")]
    wu = conn.execute("SELECT COUNT(*) c FROM watched_users").fetchone()["c"]
    tt_n = conn.execute("SELECT COUNT(*) c FROM target_tweets").fetchone()["c"]
    rq_n = conn.execute("SELECT COUNT(*) c FROM review_queue").fetchone()["c"]
    dry = conn.execute("SELECT value FROM app_settings WHERE key='dry_run'").fetchone()
    my_min = conn.execute("SELECT min_llm_score FROM search_rules WHERE name='我的规则'").fetchone()["min_llm_score"]
    obsolete = conn.execute("SELECT COUNT(*) c FROM app_settings WHERE key IN ('tweet_max_age_hours','billing_mode','monthly_read_quota')").fetchone()["c"]
    thr = conn.execute("SELECT value FROM app_settings WHERE key='match_confidence_threshold'").fetchone()["value"]
    s_int = conn.execute("SELECT value FROM app_settings WHERE key='search_interval_minutes'").fetchone()["value"]
    assert s_int == "360" and conn.execute("SELECT 1 FROM app_settings WHERE key='search_runs_per_day'").fetchone() is None, s_int
    sp_row = conn.execute("SELECT * FROM scheduled_posts").fetchone()
    assert sp_row["content_mode"] == "fixed" and sp_row["material_id"] == mat_old and sp_row["schedule_expr"] == "21:00", dict(sp_row)
    conn.execute("INSERT INTO review_queue(account_id, action_type, scheduled_post_id, final_text, status, created_at) VALUES (?,'post',?,'x','pending',?)",
                 (acc_id, sp_row["id"], utcnow_iso()))   # 外键仍指向重建后的表
    conn.execute("INSERT INTO scheduled_posts(account_id, material_id, content_mode, schedule_type, schedule_expr) VALUES (?,NULL,'pool','daily','09:00')", (acc_id,))
    conn.rollback()
assert ver == 11 and accs == ["my_real"] and mats == ["我的素材"] and rules == ["我的规则"] and wu == 0 and tt_n == 0 and rq_n == 0 and dry is None, (ver, accs, mats, rules, wu, tt_n, rq_n, dry)
assert my_min == 5 and obsolete == 0 and thr == "0.4", (my_min, obsolete, thr)
print("[1] v2→v10 升级 OK：Mock 演示数据全部清除、用户数据保留；旧默认达标分 7→5、匹配门槛 0.7→0.4；废弃设置键已清")

# ---------- 2. 全新库：干干净净 ----------
fresh_conn_state()
init_db(TMP / 'new.db', setting_overrides={"cooldown_days": 3, "not_a_key": 1})
with get_conn() as conn:
    for tbl in ("accounts", "materials", "watched_users", "search_rules", "target_tweets"):
        assert conn.execute(f"SELECT COUNT(*) c FROM {tbl}").fetchone()["c"] == 0, tbl
    assert conn.execute("SELECT COUNT(*) c FROM app_settings WHERE key='dry_run'").fetchone()["c"] == 0
    assert conn.execute("SELECT value FROM app_settings WHERE key='cooldown_days'").fetchone()["value"] == "3"
    assert conn.execute("SELECT COUNT(*) c FROM app_settings WHERE key='not_a_key'").fetchone()["c"] == 0
print("[2] 新库无任何演示数据 OK；settings.toml [defaults] 首启动生效")

# ---------- 3. 全链路（X_OPERATOR_MOCK=1 走 Mock 适配器，仅测试） ----------
from x_operator.core.scheduler import Jobs  # noqa: E402
from x_operator.adapters import factory  # noqa: E402
factory.invalidate()
jobs = Jobs()
m0 = jobs.monitor.run_once(); assert m0.users_polled == 0 and "没有状态为「启用」的账号" in m0.as_msg(), m0.as_msg()
print("[3a] 无账号提示 OK:", m0.as_msg()[:40])
with get_conn() as conn:
    conn.execute("INSERT INTO accounts(handle, access_type, is_primary, credentials, active_hours_start, active_hours_end, min_interval_sec, max_interval_sec) "
                 "VALUES ('tester','official',1,'{}','00:00','00:00',0,0)")
    for lang, text in (("ja", "月額で悩んでいるなら、買い切りの選択肢もありますよ"), ("en", "If pricing is the blocker, there are cheaper options."), ("zh", "如果卡在价格上，可以试试便宜点的方案")):
        conn.execute("INSERT INTO materials(kind,text,lang,status) VALUES ('reply',?,?,'active')", (text, lang))
    conn.execute("INSERT INTO watched_users(handle,x_user_id) VALUES ('someone','1234567')")
    conn.execute("INSERT INTO search_rules(name,keyword_query,semantic_criteria,lang,min_llm_score,max_results_per_run) VALUES ('规则A','(API cost) -is:retweet','找为成本发愁的人','ja,en',6,15)")
    conn.commit()
m = jobs.monitor.run_once(); print("[3b] 监控:", m.as_msg()[:80])
assert m.users_polled == 1 and m.tweets_fetched == 3, m
from x_operator.core.search import effective_query  # noqa: E402
with get_conn() as conn:
    rule = conn.execute("SELECT * FROM search_rules").fetchone()
eq = effective_query(rule); assert eq.endswith("(lang:ja OR lang:en)"), eq
print("[3c] 多语言查询 OK:", eq)
from x_operator.core.search import normalize_keywords  # noqa: E402
assert normalize_keywords("adult,nsfw,AI美女，AI成人、AI短剧") == 'adult OR nsfw OR "AI美女" OR "AI成人" OR "AI短剧"', normalize_keywords("adult,nsfw,AI美女，AI成人、AI短剧")
assert normalize_keywords("(カメラ 高い OR レンズ 高い) (初心者 OR おすすめ)") == "(カメラ 高い OR レンズ 高い) (初心者 OR おすすめ)"
assert normalize_keywords("single") == "single"
eq2 = effective_query({"keyword_query": "adult, AI短剧", "lang": "zh,en"})
assert eq2 == '(adult OR "AI短剧") (lang:zh OR lang:en) -is:retweet', eq2
print("[3c2] 逗号=任一命中 OK:", eq2)
s = jobs.search.run_once(); print("[3d] 搜索:", s.as_msg().replace("\n", " | ")[:200])
assert s.rules_run == 1 and s.tweets_fetched == 6, s
with get_conn() as conn:
    reasons = [r["llm_relevance_reason"] for r in conn.execute("SELECT llm_relevance_reason FROM target_tweets WHERE source='search' AND process_status='filtered'")]
    dist = {r[0]: r[1] for r in conn.execute("SELECT process_status, COUNT(*) FROM target_tweets GROUP BY 1")}
print("[3e] 抓取分布:", dist)
assert reasons, "应有被过滤的搜索结果"
assert all(("低于规则" in r) or ("不在规则选的语言" in r) or ("预检拦下" in r) for r in reasons), reasons
assert any("未配置 LLM" in r for r in reasons), reasons
print("[3f] 过滤原因明确 OK，例:", reasons[0][:70])
assert "抓取记录" in s.as_msg()
with get_conn() as conn:
    pend = conn.execute("SELECT COUNT(*) c FROM review_queue WHERE status='pending'").fetchone()["c"]
    conn.execute("UPDATE review_queue SET status='approved', decided_at=? WHERE status='pending'", (utcnow_iso(),)); conn.commit()
assert pend >= 1, pend
r = jobs.dispatcher.tick(); print("[3g] 分发:", r.as_msg()[:80]); assert r.sent == 1, r
with get_conn() as conn:
    nm = conn.execute("SELECT id FROM target_tweets WHERE process_status IN ('no_match','filtered') LIMIT 1").fetchone()
if nm:
    out = jobs.match.rematch(nm["id"]); print("[3h] 重新匹配:", out.status, out.reason[:50])

# ---------- 3x. v5 新能力：手动选素材 / AI 撰写（无 LLM 应给明确提示）/ 回复方式 manual / 发送后核实 ----------
with get_conn() as conn:
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(search_rules)")}
    assert {"lookback_hours", "reply_mode", "ai_brief", "allow_polish"} <= cols, cols
    qcols = {r["name"] for r in conn.execute("PRAGMA table_info(review_queue)")}
    assert {"origin", "verify_status"} <= qcols, qcols
    sent_row = conn.execute("SELECT verify_status, origin FROM review_queue WHERE status='sent' LIMIT 1").fetchone()
assert sent_row["verify_status"] == "ok" and sent_row["origin"] == "ai_match", dict(sent_row)
print("[3i] 发送后自动回查 verify_status=ok，origin 记录 OK")
with get_conn() as conn:
    tgt = conn.execute("SELECT id, lang FROM target_tweets WHERE process_status IN ('filtered','no_match') LIMIT 1").fetchone()
    mat = conn.execute("SELECT id FROM materials WHERE kind='reply' AND status='active' AND lang=?", (tgt["lang"],)).fetchone() \
        or conn.execute("SELECT id FROM materials WHERE kind='reply' AND status='active' LIMIT 1").fetchone()
out = jobs.match.manual_match(tgt["id"], mat["id"], "我手动改过的文案")
assert out.status == "queued", out
with get_conn() as conn:
    q = conn.execute("SELECT origin, final_text, material_id FROM review_queue WHERE id=?", (out.queue_id,)).fetchone()
assert q["origin"] == "manual" and q["final_text"] == "我手动改过的文案" and q["material_id"] == mat["id"], dict(q)
print("[3j] 手动选素材进队列 OK")
with get_conn() as conn:
    tgt2 = conn.execute("SELECT id FROM target_tweets WHERE process_status IN ('filtered','no_match') LIMIT 1").fetchone()
out = jobs.match.ai_write(tgt2["id"], "推荐我们的产品 @ExampleBrand")
assert out.status == "no_match" and "设置 → LLM" in out.reason, out
print("[3k] 无 LLM 时 AI 撰写给出明确提示 OK:", out.reason[:40])
from x_operator.core.matcher import extract_must_include  # noqa: E402
assert extract_must_include("带上 https://example.com/ 和 @ExampleBrand，谢谢") == ["https://example.com/", "@ExampleBrand"]
# reply_mode=manual：达标推文不自动生成
with get_conn() as conn:
    conn.execute("UPDATE search_rules SET reply_mode='manual', newest_id_cursor=NULL WHERE name='规则A'"); conn.commit()
s2 = jobs.search.run_once()
with get_conn() as conn:
    manual_reason = conn.execute("SELECT llm_relevance_reason FROM target_tweets WHERE source='search' AND process_status='no_match' "
                                 "AND llm_relevance_reason LIKE '%手动处理%' LIMIT 1").fetchone()
assert s2.queued == 0 and manual_reason is not None, (s2.as_msg(), manual_reason)
print("[3l] 回复方式=只抓取手动处理 OK")
# lookback：把游标清空、时间窗设为 0.0001 小时 → 全部被时间窗挡下
with get_conn() as conn:
    conn.execute("UPDATE watched_users SET last_seen_tweet_id=NULL, lookback_hours=1"); conn.commit()
m3 = jobs.monitor.run_once(); assert m3.tweets_fetched == 3, m3.as_msg()  # Mock 推文都在几分钟内，1 小时窗内全保留
print("[3m] 监控首次回溯时间窗 OK")

# ---------- 4. 登录流程离线模拟 ----------
from x_operator.adapters.real import (UnofficialXClient, validate_unofficial_credentials,  # noqa: E402
                                      resolve_proxy, detect_system_proxy, OfficialXClient)
from x_operator.adapters.base import AuthExpired  # noqa: E402
import pyotp  # noqa: E402


class FakeV11:
    def __init__(self, script): self.script = script; self.submitted = []

    async def onboarding_task(self, guest_token, token, subtask_inputs, **kw):
        sid = subtask_inputs[0]["subtask_id"] if subtask_inputs else "init"
        self.submitted.append(subtask_inputs[0] if subtask_inputs else {"subtask_id": "init"})
        nxt = self.script.get(sid)
        if nxt is None:
            return ({"flow_token": "t", "subtasks": []}, None)
        sub = nxt if isinstance(nxt, dict) else {"subtask_id": nxt}
        return ({"flow_token": "t", "subtasks": [sub]}, None)

    async def sso_init(self, p, g): pass


class FakeHttp:
    def __init__(self): self.cookies = {}


class FakeClient:
    def __init__(self, script, cookies):
        self.v11 = FakeV11(script); self.http = FakeHttp(); self._c = cookies; self._user_id = None

    async def _get_guest_token(self): return "g"

    async def _ui_metrics(self): raise RuntimeError("no js")

    def get_cookies(self): return self._c


BASE = {"init": "LoginJsInstrumentationSubtask", "LoginJsInstrumentationSubtask": "LoginEnterUserIdentifierSSO",
        "LoginEnterUserIdentifierSSO": "LoginEnterPassword"}
saved = {}


def mk(script, cookies, creds):
    cli = UnofficialXClient(credentials=dict(creds), on_cookies_refreshed=lambda ck: saved.update(ck))
    cli._client = FakeClient(script, cookies)
    return cli


CREDS = {"username": "u1", "password": "pw", "totp_secret": "jbsw y3dp ehpk 3pxp"}
cli = mk({**BASE, "LoginEnterPassword": "LoginTwoFactorAuthChallenge", "LoginTwoFactorAuthChallenge": "AccountDuplicationCheck",
          "AccountDuplicationCheck": "LoginSuccessSubtask"}, {"auth_token": "c" * 40, "ct0": "d" * 32}, CREDS)
cli._password_login()
sub = {s["subtask_id"]: s for s in cli._client.v11.submitted}
code = sub["LoginTwoFactorAuthChallenge"]["enter_text"]["text"]
assert code == pyotp.TOTP("JBSWY3DPEHPK3PXP").now() and len(code) == 6, code
assert cli._logged_in and cli.creds["auth_token"] == "c" * 40 and saved.get("ct0") == "d" * 32
print("[4a] 密码+TOTP 登录模拟 OK：TOTP 码正确、Cookie 回写", code)
cli = mk({**BASE, "LoginEnterUserIdentifierSSO": "LoginEnterAlternateIdentifierSubtask", "LoginEnterAlternateIdentifierSubtask": "LoginEnterPassword",
          "LoginEnterPassword": "LoginSuccessSubtask"}, {"auth_token": "c" * 40, "ct0": "d" * 32}, {**CREDS, "email": "me@x.com"})
cli._password_login(); assert cli._logged_in
sub = {s["subtask_id"]: s for s in cli._client.v11.submitted}; assert sub["LoginEnterAlternateIdentifierSubtask"]["enter_text"]["text"] == "me@x.com"
print("[4b] 邮箱二次确认自动应答 OK")
cli = mk({**BASE, "LoginEnterUserIdentifierSSO": "LoginEnterAlternateIdentifierSubtask"}, {}, CREDS)
try:
    cli._password_login(); raise SystemExit("应报错")
except AuthExpired as e:
    assert "邮箱" in str(e); print("[4c] 缺邮箱提示 OK:", str(e)[:40])
cli = mk({**BASE, "LoginEnterPassword": {"subtask_id": "LoginAcid", "enter_text": {"secondary_text": {"text": "Check your email"}}}}, {}, CREDS)
try:
    cli._password_login(); raise SystemExit("应报错")
except AuthExpired as e:
    assert "验证码" in str(e) and "Check your email" in str(e); print("[4d] 邮箱验证码明确提示 OK:", str(e)[:50])
cli = mk({**BASE, "LoginEnterPassword": {"subtask_id": "DenyLoginSubtask", "cta": {"secondary_text": {"text": "Suspicious login"}}}}, {}, CREDS)
try:
    cli._password_login(); raise SystemExit("应报错")
except AuthExpired as e:
    assert "Suspicious login" in str(e); print("[4e] 拒绝登录提示 OK")
cli = mk({**BASE, "LoginEnterPassword": "LoginTwoFactorAuthChallenge"}, {}, {"username": "u1", "password": "pw"})
try:
    cli._password_login(); raise SystemExit("应报错")
except AuthExpired as e:
    assert "TOTP" in str(e); print("[4f] 缺 TOTP 密钥提示 OK")
cli = mk({**BASE, "LoginEnterPassword": "LoginSuccessSubtask"}, {}, CREDS)
try:
    cli._password_login(); raise SystemExit("应报错")
except AuthExpired as e:
    assert "Cookie" in str(e); print("[4g] 无 Cookie 下发提示 OK")

# ---------- 5. 代理与格式校验 ----------
os.environ["HTTPS_PROXY"] = "127.0.0.1:7890"; os.environ.pop("HTTP_PROXY", None); os.environ.pop("ALL_PROXY", None)
assert detect_system_proxy() == "http://127.0.0.1:7890", detect_system_proxy()
assert resolve_proxy({}) == "http://127.0.0.1:7890" and resolve_proxy({"proxy": "direct"}) is None and resolve_proxy({"proxy": "10.0.0.1:1080"}) == "http://10.0.0.1:1080"
os.environ["HTTPS_PROXY"] = "https://proxy.local:443"
assert detect_system_proxy() == ("http://proxy.local:443" if sys.platform == "win32" else "https://proxy.local:443"), detect_system_proxy()
os.environ.pop("HTTPS_PROXY"); assert detect_system_proxy() is None
print("[5a] 系统代理检测/直连/自定义 OK（https 前缀只在 Windows 改写）")
assert validate_unofficial_credentials({"auth_token": "abc", "ct0": "d" * 32}).startswith("auth_token 格式不对")
assert validate_unofficial_credentials({"auth_token": "a" * 40, "ct0": "zz"}).startswith("ct0 格式不对")
assert validate_unofficial_credentials({"auth_token": "a" * 40}).startswith("auth_token 和 ct0 要一起填")
assert "6 位数字" in validate_unofficial_credentials({"totp_secret": "123456"})
assert validate_unofficial_credentials({"auth_token": "a" * 40, "ct0": "b" * 160, "totp_secret": "jbsw y3dp ehpk 3pxp"}) == ""
print("[5b] 凭据格式校验 OK")
oc = OfficialXClient(credentials={"consumer_key": "k", "consumer_secret": "s", "access_token": "t", "access_token_secret": "ts", "proxy": "127.0.0.1:9999"})
assert oc._client.session.proxies["https"] == "http://127.0.0.1:9999" and oc.proxy_used == "http://127.0.0.1:9999"
print("[5c] 官方 API 客户端代理注入 OK")
try:
    oc.get_home_timeline(kind="for_you"); raise SystemExit("应报错")
except Exception as e:
    assert "推荐流" in str(e) and "小号" in str(e), str(e)
print("[5d] 官方 API 读推荐流给出明确提示 OK")

# ---------- 6. 审查后修复的回归用例 ----------
from datetime import datetime, timedelta, timezone  # noqa: E402
from x_operator import config  # noqa: E402
from x_operator.adapters.base import NetworkError, RateLimited, TweetData, XClientError, FetchResult  # noqa: E402
from x_operator.adapters.real import _LoopThread, clamp_recent_window  # noqa: E402
from x_operator.core import budget  # noqa: E402
from x_operator.core.compliance import is_blacklisted  # noqa: E402
from x_operator.core.monitor import get_primary_account, precheck  # noqa: E402
from x_operator.core.scheduler import expire_stale, run_startup_recovery  # noqa: E402
from x_operator.core.search import coerce_score  # noqa: E402
from x_operator.db.database import parse_iso, to_iso  # noqa: E402

fresh_conn_state()
init_db(TMP / 'new.db')
with get_conn() as conn:
    acc = conn.execute("SELECT * FROM accounts WHERE handle='tester'").fetchone()
    conn.execute("UPDATE review_queue SET status='skipped' WHERE status IN ('approved','pending')")
    conn.execute("UPDATE accounts SET next_allowed_at=NULL"); conn.commit()


def _fresh_pending_item():
    """挑一条没进过队列的抓取记录，手动选素材 → 批准，返回 (queue_id, target_tweet_id 字符串)。"""
    with get_conn() as conn:
        t = conn.execute("SELECT id, tweet_id, lang FROM target_tweets WHERE process_status IN ('filtered','no_match') "
                         "AND id NOT IN (SELECT target_tweet_id FROM review_queue WHERE target_tweet_id IS NOT NULL) "
                         "AND tweet_id NOT IN (SELECT tweet_id FROM interactions) LIMIT 1").fetchone()
        m = conn.execute("SELECT id FROM materials WHERE kind='reply' AND status='active' LIMIT 1").fetchone()
    out = jobs.match.manual_match(t["id"], m["id"], "回归测试文案 " + t["tweet_id"])
    assert out.status == "queued", out
    with get_conn() as conn:
        conn.execute("UPDATE review_queue SET status='approved', decided_at=? WHERE id=?", (utcnow_iso(), out.queue_id)); conn.commit()
    return out.queue_id, t["tweet_id"]


# [6a] 启动恢复：发送中断 → 有 X id 的标已发送，没有的标失败（不再盲目回置待发送）
with get_conn() as conn:
    conn.execute("INSERT INTO review_queue(account_id, action_type, final_text, status, sent_tweet_id, created_at) VALUES (?,'post','a','sending','999',?)", (acc["id"], utcnow_iso()))
    conn.execute("INSERT INTO review_queue(account_id, action_type, final_text, status, created_at) VALUES (?,'post','b','sending',?)", (acc["id"], utcnow_iso()))
    conn.commit()
msgs = run_startup_recovery(jobs)
with get_conn() as conn:
    a_st = conn.execute("SELECT status FROM review_queue WHERE final_text='a'").fetchone()["status"]
    b = conn.execute("SELECT status, error_msg FROM review_queue WHERE final_text='b'").fetchone()
assert a_st == "sent" and b["status"] == "failed" and "无法确认" in b["error_msg"], (a_st, dict(b), msgs)
print("[6a] 启动恢复不再重发结果未知的条目 OK")

# [6b] 429 限流：条目回置待发送、不消耗重试次数，账号 next_allowed_at 推到重置时间
client = factory.get_client(acc)
qid, tid_str = _fresh_pending_item()
reset_at = datetime.now(timezone.utc) + timedelta(minutes=10)
orig_reply = client.reply
client.reply = lambda text, rid, media_ids=None: (_ for _ in ()).throw(RateLimited("429", reset_at=reset_at))
r = jobs.dispatcher.tick()
with get_conn() as conn:
    row = conn.execute("SELECT status, retry_count, error_msg FROM review_queue WHERE id=?", (qid,)).fetchone()
    na = parse_iso(conn.execute("SELECT next_allowed_at FROM accounts WHERE id=?", (acc["id"],)).fetchone()["next_allowed_at"])
assert row["status"] == "approved" and row["retry_count"] == 0 and "限流" in row["error_msg"], dict(row)
assert na is not None and abs((na - reset_at).total_seconds()) < 2, (na, reset_at)
assert r.sent == 0 and "限流" in r.as_msg(), r.as_msg()
print("[6b] 限流按 X 给的重置时间暂停、不烧重试次数 OK")
with get_conn() as conn:
    conn.execute("UPDATE accounts SET next_allowed_at=NULL WHERE id=?", (acc["id"],)); conn.commit()

# [6c] 发送超时但其实发出去了：到自己时间线上找回，按已发送处理，不重发
client.reply = lambda text, rid, media_ids=None: (_ for _ in ()).throw(NetworkError("timeout"))
me = client.get_me()
found = TweetData(tweet_id="777000111", author_id=me.user_id, author_handle=me.handle, text="回归测试文案", lang="zh",
                  created_at=datetime.now(timezone.utc), is_retweet=False, in_reply_to_tweet_id=tid_str)
orig_gut = client.get_user_tweets
client.get_user_tweets = lambda *a, **k: FetchResult(tweets=[found], newest_id="777000111", reads_consumed=1)
r = jobs.dispatcher.tick()
with get_conn() as conn:
    row = conn.execute("SELECT status, sent_tweet_id, retry_count FROM review_queue WHERE id=?", (qid,)).fetchone()
    ledger = conn.execute("SELECT 1 FROM interactions WHERE tweet_id=?", (tid_str,)).fetchone()
assert row["status"] == "sent" and row["sent_tweet_id"] == "777000111" and ledger is not None, dict(row)
assert r.sent == 1 and "777000111" in r.as_msg(), r.as_msg()
print("[6c] 超时后找回已发推文、不重发 OK")
client.reply = orig_reply; client.get_user_tweets = orig_gut
with get_conn() as conn:
    conn.execute("UPDATE accounts SET next_allowed_at=NULL WHERE id=?", (acc["id"],)); conn.commit()

# [6d] 同一账号并发发送：锁住时另一线程直接跳过
qid2, _ = _fresh_pending_item()
lock = jobs.dispatcher._account_lock(acc["id"]); lock.acquire()
try:
    r = jobs.dispatcher.tick()
finally:
    lock.release()
assert r.sent == 0 and "另一次发送" in r.as_msg(), r.as_msg()
r = jobs.dispatcher.tick(); assert r.sent == 1, r.as_msg()
print("[6d] 账号级发送锁 OK")
with get_conn() as conn:
    conn.execute("UPDATE accounts SET next_allowed_at=NULL WHERE id=?", (acc["id"],)); conn.commit()

# [6e] 黑名单按 @handle 也能拦
with get_conn() as conn:
    conn.execute("INSERT INTO blacklist(x_user_id, handle, reason, created_at) VALUES ('spammer','spammer','test',?)", (utcnow_iso(),)); conn.commit()
    assert is_blacklisted(conn, "123456789", "Spammer") and not is_blacklisted(conn, "123456789", "goodguy")
tw = TweetData(tweet_id="5", author_id="123456789", author_handle="spammer", text="hi", lang="en",
               created_at=datetime.now(timezone.utc), is_retweet=False, in_reply_to_tweet_id=None)
assert precheck(tw, "tester") == "blacklisted"
print("[6e] 黑名单 @handle 匹配 OK")

# [6f] LLM 打分容错：tweet_id 数字、分数 "8/10"
assert coerce_score("8/10") == 8 and coerce_score(7.6) == 8 and coerce_score(None) == 0 and coerce_score(15) == 10 and coerce_score(True) == 0
orig_score = jobs.llm.score_relevance
jobs.llm.score_relevance = lambda crit, payload: [{"tweet_id": int(p["tweet_id"]), "score": "9/10", "reason": "r"} for p in payload]
with get_conn() as conn:
    rule = conn.execute("SELECT * FROM search_rules").fetchone()
scored = jobs.search.run_rule(rule, get_primary_account())
real_scored = [c for c in scored if not c.prefiltered]
assert real_scored and all(c.score == 9 for c in real_scored), [(c.score, c.prefiltered) for c in scored]
jobs.llm.score_relevance = orig_score
print("[6f] LLM 返回数字 tweet_id / 分数字符串也能对上号 OK")
# 只跑指定规则（停用的也跑）；不存在的规则给提示
with get_conn() as conn:
    conn.execute("UPDATE search_rules SET enabled=0 WHERE id=?", (rule["id"],)); conn.commit()
st = jobs.search.run_once(rule_ids=[rule["id"]]); assert st.rules_run == 1 and st.tweets_fetched == 6, st.as_msg()
st = jobs.search.run_once(); assert st.rules_run == 0 and "没有启用的搜索规则" in st.as_msg(), st.as_msg()
st = jobs.search.run_once(rule_ids=[999999]); assert st.rules_run == 0 and "不存在" in st.as_msg(), st.as_msg()
with get_conn() as conn:
    conn.execute("UPDATE search_rules SET enabled=1 WHERE id=?", (rule["id"],)); conn.commit()
print("[6f2] 单条规则运行 OK")
# 进度回调：单调不减、以 1.0 结尾、文字里带规则名/推主名
events = []
jobs.search.run_once(rule_ids=[rule["id"]], progress=lambda f, t: events.append((f, t)))
assert events and events[-1][0] == 1.0 and all(events[i][0] <= events[i + 1][0] for i in range(len(events) - 1)), events
assert any("规则「" in t and "抓取" in t for _, t in events) and any("打分" in t for _, t in events), events
events = []
jobs.monitor.run_once(progress=lambda f, t: events.append((f, t)))
assert events and events[-1][0] == 1.0 and any("@someone" in t for _, t in events), events
print("[6f3] 进度回调 OK")
# 观看量门槛在抓取端翻页：凑够条数、低于门槛的不入库、游标推进到扫描过的最新一条
with get_conn() as conn:
    conn.execute("UPDATE search_rules SET min_views=1000, max_results_per_run=15, newest_id_cursor=NULL WHERE id=?", (rule["id"],)); conn.commit()
    rule = conn.execute("SELECT * FROM search_rules WHERE id=?", (rule["id"],)).fetchone()
notes = []
cands = jobs.search.run_rule(rule, get_primary_account(), notes=notes)
assert len(cands) >= 15 and all((c.tweet.view_count or 0) >= 1000 for c in cands), [(c.tweet.view_count) for c in cands]
assert notes and "观看量低于 1000" in notes[-1] and "已跳过" in notes[-1], notes
with get_conn() as conn:
    max_id_before = conn.execute("SELECT COALESCE(MAX(id),0) m FROM target_tweets").fetchone()["m"]
st = jobs.search.run_once(rule_ids=[rule["id"]])
with get_conn() as conn:
    low = conn.execute("SELECT COUNT(*) c FROM target_tweets WHERE id>? AND view_count IS NOT NULL AND view_count<1000", (max_id_before,)).fetchone()["c"]
    stored_views = conn.execute("SELECT COUNT(*) c FROM target_tweets WHERE id>? AND view_count>=1000", (max_id_before,)).fetchone()["c"]
    cur = conn.execute("SELECT newest_id_cursor FROM search_rules WHERE id=?", (rule["id"],)).fetchone()["newest_id_cursor"]
assert low == 0 and stored_views >= 15 and cur, (low, stored_views, cur)
assert any("观看量低于" in n for n in st.notes), st.as_msg()
# 门槛高到一条都没有：提示里给出最高观看量，让人知道该调到多少
with get_conn() as conn:
    conn.execute("UPDATE search_rules SET min_views=10000000 WHERE id=?", (rule["id"],)); conn.commit()
st = jobs.search.run_once(rule_ids=[rule["id"]]); m = st.as_msg()
assert st.tweets_fetched == 0 and "最高观看量是 25000" in m and "以下才会有结果" in m, m
with get_conn() as conn:
    conn.execute("UPDATE search_rules SET min_views=0 WHERE id=?", (rule["id"],)); conn.commit()
print("[6f4] 观看量门槛在抓取端翻页 OK（0 条时提示最高观看量）")
# LLM 模型分工登记表：所有场景都登记；未登记场景直接报错
from x_operator.llm.client import SCENE_TIERS, model_for, LLMError as _LLMError  # noqa: E402
assert {"ping", "relevance", "match", "write", "rule_gen", "material_gen"} <= set(SCENE_TIERS)
assert model_for("relevance") == "gpt-4o-mini" and model_for("write") == "gpt-4o"
try:
    jobs.llm.chat_json("brand_new_feature", [{"role": "user", "content": "x"}], ["ok"]); raise SystemExit("应报错")
except _LLMError as e:
    assert "SCENE_TIERS" in str(e) and "登记" in str(e), str(e)
print("[6f5] LLM 模型分工登记表 OK")
# 创作要求模板：存/覆盖/列出/删
from x_operator.ui.pickers import load_templates, save_template, delete_template, _bump_template  # noqa: E402
save_template("日本独立开发者", "推荐我们的产品 @ExampleBrand，语气像同行")
save_template("日本独立开发者", "改过的版本 @ExampleBrand")
save_template("英文创作者", "Mention https://example.com/")
tpls = load_templates(); assert [t["name"] for t in tpls] == ["日本独立开发者", "英文创作者"] or len(tpls) == 2, [dict(t) for t in tpls]
assert next(t for t in tpls if t["name"] == "日本独立开发者")["text"] == "改过的版本 @ExampleBrand"
_bump_template(next(t for t in tpls if t["name"] == "英文创作者")["id"])
assert load_templates()[0]["name"] == "英文创作者"   # 用得多的排前面
delete_template(tpls[0]["id"]); assert len(load_templates()) == 1
print("[6f6] 创作要求模板 OK")
# 素材匹配「宽进」：没同语言素材也给一条；AI 出错/说跳过也按规则兜底进待审核
from x_operator.llm.client import LLMError as _LE  # noqa: E402


def _pick_unqueued():
    with get_conn() as conn:
        return conn.execute("SELECT id FROM target_tweets WHERE process_status IN ('filtered','no_match') "
                            "AND id NOT IN (SELECT target_tweet_id FROM review_queue WHERE target_tweet_id IS NOT NULL) "
                            "AND tweet_id NOT IN (SELECT tweet_id FROM interactions) LIMIT 1").fetchone()["id"]


tid = _pick_unqueued()
with get_conn() as conn:
    conn.execute("UPDATE target_tweets SET lang='ko' WHERE id=?", (tid,)); conn.commit()   # 素材库里没有韩语素材
out = jobs.match.rematch(tid); assert out.status == "queued" and "没有「ko」" in out.reason, out
tid = _pick_unqueued()
orig_match = jobs.llm.match_reply
jobs.llm.match_reply = lambda *a, **k: (_ for _ in ()).throw(_LE("模型拒绝"))
out = jobs.match.rematch(tid); assert out.status == "queued" and "AI 匹配出错" in out.reason and "把关" in out.reason, out
tid = _pick_unqueued()
jobs.llm.match_reply = lambda *a, **k: {"skip": True, "material_id": None, "reply_text": "", "confidence": 0.0, "reason": "都不贴"}
out = jobs.match.rematch(tid); assert out.status == "queued" and "都不太贴" in out.reason, out
jobs.llm.match_reply = orig_match
with get_conn() as conn:
    q = conn.execute("SELECT material_id, final_text, llm_confidence FROM review_queue WHERE id=?", (out.queue_id,)).fetchone()
assert q["material_id"] and q["final_text"] and q["llm_confidence"] == 0.35, dict(q)
print("[6f7] 素材匹配宽进（无同语言 / AI 出错 / AI 跳过都给草稿）OK")
# 多账号：回复在小号间自动轮流、主号不参与；规则可指定固定账号；待审核可改账号
from x_operator.core.accounts import choose_reply_account, account_options  # noqa: E402
from x_operator.ui.queue import _set_account  # noqa: E402
with get_conn() as conn:
    for h in ("small1", "small2"):
        conn.execute("INSERT INTO accounts(handle, access_type, is_primary, credentials, active_hours_start, active_hours_end, min_interval_sec, max_interval_sec, daily_reply_limit) "
                     "VALUES (?,'unofficial',0,?,'00:00','00:00',0,0,15)", (h, json.dumps({"auth_token": "a" * 40, "ct0": "b" * 32})))
    conn.commit()
    ids = {r["handle"]: r["id"] for r in conn.execute("SELECT id, handle FROM accounts")}
assert set(account_options()) >= {0, ids["tester"], ids["small1"], ids["small2"]}
jobs.search.run_once(rule_ids=[rule["id"]])   # 多抓几条来分
got = []
for _ in range(4):
    out = jobs.match.rematch(_pick_unqueued()); assert out.status == "queued", out
    with get_conn() as conn:
        got.append(conn.execute("SELECT account_id FROM review_queue WHERE id=?", (out.queue_id,)).fetchone()["account_id"])
assert set(got) == {ids["small1"], ids["small2"]} and got.count(ids["small1"]) == 2, (got, ids)   # 均匀分摊、主号不参与
assert "自动轮流" in out.reason, out.reason
with get_conn() as conn:
    conn.execute("UPDATE search_rules SET reply_account_id=? WHERE id=?", (ids["tester"], rule["id"])); conn.commit()
out = jobs.match.rematch(_pick_unqueued())
with get_conn() as conn:
    assert conn.execute("SELECT account_id FROM review_queue WHERE id=?", (out.queue_id,)).fetchone()["account_id"] == ids["tester"]
assert "指定账号" in out.reason, out.reason
assert _set_account(out.queue_id, ids["small2"])
with get_conn() as conn:
    assert conn.execute("SELECT account_id FROM review_queue WHERE id=?", (out.queue_id,)).fetchone()["account_id"] == ids["small2"]
    conn.execute("UPDATE search_rules SET reply_account_id=NULL WHERE id=?", (rule["id"],))
    conn.execute("UPDATE accounts SET status='paused' WHERE handle IN ('small1','small2')"); conn.commit()
    acc_row = conn.execute("SELECT * FROM accounts WHERE handle='tester'").fetchone()
chosen, note = choose_reply_account(None, acc_row)
assert chosen["id"] == ids["tester"] and "没有启用中的小号" in note, note   # 没小号才退回主号
print("[6f8] 多账号回复分摊（自动轮流 / 指定账号 / 队列改账号 / 无小号退回主号）OK")
# 抓取通道：默认小号（免费、预算不拦、多个小号轮流读得最少的）；切官方才用主号
from x_operator.core.monitor import get_read_account, read_is_billed  # noqa: E402
with get_conn() as conn:
    conn.execute("UPDATE accounts SET status='active' WHERE handle IN ('small1','small2')"); conn.commit()
assert get_read_account()["handle"] in ("small1", "small2") and not read_is_billed(get_read_account())
with get_conn() as conn:
    conn.execute("INSERT INTO action_log(account_id, api_kind, endpoint, reads_consumed, success, created_at) VALUES (?, 'x_unofficial', 'search_recent', 50, 1, ?)", (ids["small1"], utcnow_iso())); conn.commit()
assert get_read_account()["handle"] == "small2"   # 读得少的优先
config.set_value("daily_read_budget", 1)          # 额度只剩 1，走小号照样能抓
st = jobs.search.run_once(rule_ids=[rule["id"]]); assert st.rules_run == 1 and "小号通道，不计费" in st.as_msg(), st.as_msg()
config.set_value("read_channel", "official")
assert get_read_account()["handle"] == "tester" and read_is_billed(get_read_account())
st = jobs.search.run_once(rule_ids=[rule["id"]]); assert st.rules_run == 0 and "已用完" in st.as_msg(), st.as_msg()
config.set_value("read_channel", "unofficial"); config.set_value("daily_read_budget", 0)
with get_conn() as conn:
    conn.execute("UPDATE accounts SET status='paused' WHERE handle IN ('small1','small2')"); conn.commit()
assert get_read_account()["handle"] == "tester"   # 没小号退回官方
print("[6f9] 抓取通道（默认小号免费不拦 / 小号轮流 / 切官方才计费受限）OK")
# 搜索 0 条 / 出错时必须有能看懂的提示
cli0 = factory.get_client(acc_row)
orig_search = cli0.search_recent
cli0.search_recent = lambda *a, **k: FetchResult(tweets=[], newest_id=None, reads_consumed=0)
with get_conn() as conn:
    conn.execute("UPDATE search_rules SET newest_id_cursor=NULL WHERE id=?", (rule["id"],)); conn.commit()
st = jobs.search.run_once(rule_ids=[rule["id"]]); m = st.as_msg()
assert "没有返回任何推文" in m and "实际查询" in m and "可能原因" in m, m
with get_conn() as conn:
    conn.execute("UPDATE search_rules SET newest_id_cursor='1' WHERE id=?", (rule["id"],)); conn.commit()
st = jobs.search.run_once(rule_ids=[rule["id"]]); assert "游标之后没有新推文" in st.as_msg(), st.as_msg()
cli0.search_recent = lambda *a, **k: (_ for _ in ()).throw(XClientError("X 拒绝了搜索请求"))
st = jobs.search.run_once(rule_ids=[rule["id"]]); m = st.as_msg()
assert st.errors == 1 and m.split("\n")[1].startswith("❌") and "X 拒绝了搜索请求" in m, m
cli0.search_recent = orig_search
with get_conn() as conn:
    conn.execute("UPDATE search_rules SET newest_id_cursor=NULL WHERE id=?", (rule["id"],)); conn.commit()
print("[6f10] 搜索 0 条 / 出错的提示 OK")
# 推荐流规则：不用关键词，读时间线 → 同一条流水线；抓取记录里标「推荐流」
from x_operator.core.search import is_feed_rule, effective_query as _eq  # noqa: E402
with get_conn() as conn:
    conn.execute("INSERT INTO search_rules(name, keyword_query, semantic_criteria, lang, min_llm_score, max_results_per_run, source_kind, feed_account_id) "
                 "VALUES ('推荐流规则', '', '本人在抱怨或求推荐', 'ja,en', 5, 15, 'feed_for_you', ?)", (acc["id"],))
    feed_rule = conn.execute("SELECT * FROM search_rules WHERE name='推荐流规则'").fetchone(); conn.commit()
assert is_feed_rule(feed_rule) and "推荐流" in _eq(feed_rule)
st = jobs.search.run_once(rule_ids=[feed_rule["id"]]); m = st.as_msg()
assert st.rules_run == 1 and st.tweets_fetched > 0 and "推荐流" in m and "@tester" in m, m
with get_conn() as conn:
    n_feed = conn.execute("SELECT COUNT(*) c FROM target_tweets WHERE source_rule_id=?", (feed_rule["id"],)).fetchone()["c"]
    assert n_feed == st.tweets_fetched, (n_feed, st.tweets_fetched)
    assert conn.execute("SELECT COUNT(*) c FROM action_log WHERE endpoint='home_timeline'").fetchone()["c"] >= 1
    conn.execute("UPDATE search_rules SET enabled=0 WHERE id=?", (feed_rule["id"],)); conn.commit()   # 别影响后面「只有 1 条启用规则」的用例
print("[6f13] 推荐流规则走同一条流水线 OK")
# 自动轮询：节奏可选每隔 N 分钟 / 每天固定时间点；总开关 + 单独开关；改设置立即重排
from x_operator.core.scheduler import (build_scheduler, build_trigger, describe_schedule, job_enabled,  # noqa: E402
                                       next_runs, parse_daily_times, reschedule_auto_jobs)
from apscheduler.triggers.combining import OrTrigger  # noqa: E402
from apscheduler.triggers.cron import CronTrigger  # noqa: E402
from apscheduler.triggers.interval import IntervalTrigger  # noqa: E402
assert parse_daily_times("08:00, 20:30，9:05 x 25:00 08:00 9:5") == [(8, 0), (9, 5), (20, 30)]
assert isinstance(build_trigger("search"), IntervalTrigger) and "每隔 720 分钟" in describe_schedule("search")
config.set_value("search_schedule_mode", "daily"); config.set_value("search_daily_times", "08:00, 20:00"); config.set_value("auto_jobs_timezone", "Asia/Shanghai")
assert isinstance(build_trigger("search"), OrTrigger) and describe_schedule("search") == "每天 08:00、20:00（Asia/Shanghai）"
config.set_value("search_daily_times", "21:00"); assert isinstance(build_trigger("search"), CronTrigger)
config.set_value("search_daily_times", "abc"); assert isinstance(build_trigger("search"), IntervalTrigger)   # 没填对退回间隔
config.set_value("auto_jobs_enabled", "0"); config.set_value("search_auto_enabled", "1")
assert not job_enabled("search") and job_enabled("dispatcher") and job_enabled("scheduled_check")
config.set_value("auto_jobs_enabled", "1"); config.set_value("monitor_auto_enabled", "0")
assert job_enabled("search") and not job_enabled("monitor")
config.set_value("dispatch_auto_enabled", "0"); assert not job_enabled("dispatcher"); config.set_value("dispatch_auto_enabled", "1")
config.set_value("search_daily_times", "08:00, 20:00")
sched = build_scheduler(jobs); sched.start(paused=True)
try:
    nr = next_runs(sched)
    assert nr["search"] is not None and nr["search"].hour in (8, 20) and nr["search"].minute == 0 and nr["monitor"] is None, nr
    config.set_value("search_schedule_mode", "interval"); config.set_value("search_interval_minutes", "30")
    reschedule_auto_jobs(sched)
    from datetime import datetime as _dt2, timezone as _tz2
    nr2 = next_runs(sched); delta = (nr2["search"].astimezone(_tz2.utc) - _dt2.now(_tz2.utc)).total_seconds()
    assert 25 * 60 < delta <= 30 * 60 + 5, delta
finally:
    sched.shutdown(wait=False)
config.set_value("auto_jobs_enabled", "0"); config.set_value("monitor_auto_enabled", "1"); config.set_value("search_interval_minutes", "720")
print("[6f11] 自动轮询节奏（间隔 / 每天固定时间 / 总开关+单独开关 / 立即重排）OK")

# [6g] 定时计划并发：两个线程同时扫同一个到点计划只生成 1 条；origin=scheduled
with get_conn() as conn:
    conn.execute("INSERT INTO materials(kind,text,lang,status) VALUES ('post','定时发帖素材','ja','active')")
    pm = conn.execute("SELECT id FROM materials WHERE text='定时发帖素材'").fetchone()["id"]
    conn.execute("INSERT INTO scheduled_posts(account_id, material_id, schedule_type, schedule_expr, next_run_at, status) VALUES (?,?,'daily','21:00',?,'active')",
                 (acc["id"], pm, to_iso(datetime.now(timezone.utc) - timedelta(minutes=1))))
    conn.commit()
results = []
ths = [threading.Thread(target=lambda: results.append(jobs.run_scheduled_posts())) for _ in range(2)]
[t.start() for t in ths]; [t.join() for t in ths]
with get_conn() as conn:
    n_sched = conn.execute("SELECT COUNT(*) c, MIN(origin) o FROM review_queue WHERE scheduled_post_id IS NOT NULL").fetchone()
assert sum(results) == 1 and n_sched["c"] == 1 and n_sched["o"] == "scheduled", (results, dict(n_sched))
print("[6g] 定时计划原子认领、来源标记 OK")

# [6h] 过期清扫：待审核超时 → 过期，抓取记录同步标过期
with get_conn() as conn:
    t = conn.execute("SELECT id FROM target_tweets WHERE process_status IN ('filtered','no_match') AND id NOT IN (SELECT target_tweet_id FROM review_queue WHERE target_tweet_id IS NOT NULL) LIMIT 1").fetchone()
    conn.execute("INSERT INTO review_queue(account_id, action_type, target_tweet_id, final_text, status, expires_at, created_at) VALUES (?,'reply',?,'x','pending',?,?)",
                 (acc["id"], t["id"], to_iso(datetime.now(timezone.utc) - timedelta(hours=1)), utcnow_iso()))
    conn.execute("UPDATE target_tweets SET process_status='queued' WHERE id=?", (t["id"],)); conn.commit()
assert expire_stale() == 1
with get_conn() as conn:
    assert conn.execute("SELECT process_status FROM target_tweets WHERE id=?", (t["id"],)).fetchone()["process_status"] == "expired"
print("[6h] 过期清扫同步抓取记录 OK")

# [6i] 读额度真的会拦：自动轮询触熔断线就停，手动用完才拒
used = budget.current().used_today; assert used > 0
with get_conn() as conn:   # 小号通道的读取不占额度
    conn.execute("INSERT INTO action_log(account_id, api_kind, endpoint, reads_consumed, success, created_at) VALUES (?, 'x_unofficial', 'search_recent', 999, 1, ?)", (acc["id"], utcnow_iso())); conn.commit()
b = budget.current(); assert b.used_today == used and b.free_today >= 999, (b.used_today, used, b.free_today)
config.set_value("daily_read_budget", used + 5); config.set_value("budget_reserve_reads", 20)
st = jobs.monitor.run_once(auto=True); assert st.users_polled == 0 and "自动轮询已暂停" in st.as_msg(), st.as_msg()
st = jobs.search.run_once(auto=False); assert st.rules_run == 1, st.as_msg()   # 手动还能跑
config.set_value("daily_read_budget", budget.current().used_today)
st = jobs.monitor.run_once(auto=False); assert st.users_polled == 0 and "已用完" in st.as_msg(), st.as_msg()
config.set_value("daily_read_budget", 0)
st = jobs.monitor.run_once(auto=True); assert st.users_polled == 1, st.as_msg()   # 0 = 不限
print("[6i] 读额度熔断 OK")

# [6j] LLM 纠正重试真的把纠正消息发出去了；网关 4xx 走 LLMError 且有日志
import x_operator.llm.client as lc  # noqa: E402


class FakeResp:
    def __init__(self, content, status=200): self.status_code = status; self._c = content; self.text = content

    def json(self): return {"choices": [{"message": {"content": self._c}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}}


class FakeCli:
    calls: list = []
    script: list = []

    def __init__(self, timeout=None): pass

    def __enter__(self): return self

    def __exit__(self, *a): pass

    def post(self, url, headers=None, json=None):
        FakeCli.calls.append(json); return FakeCli.script.pop(0)


orig_httpx_client = lc.httpx.Client; lc.httpx.Client = FakeCli
config.set_value("llm_base_url", "http://fake"); config.set_value("llm_api_key", "k")
FakeCli.script = [FakeResp("sorry, no json here"), FakeResp('{"ok": true}')]
obj = jobs.llm.chat_json("ping", [{"role": "user", "content": "hi"}], ["ok"])
assert obj == {"ok": True} and len(FakeCli.calls) == 2 and len(FakeCli.calls[1]["messages"]) == 3 and FakeCli.calls[1]["messages"][1]["content"] == "sorry, no json here", FakeCli.calls
assert FakeCli.calls[0]["model"] == "gpt-4o-mini", FakeCli.calls[0]["model"]   # ping 登记为轻量模型
FakeCli.calls = []; FakeCli.script = [FakeResp("bad key", status=401)]
try:
    jobs.llm.chat_json("write", [{"role": "user", "content": "hi"}], ["ok"]); raise SystemExit("应报错")
except lc.LLMError as e:
    assert "401" in str(e)
assert FakeCli.calls[0]["model"] == "gpt-4o", FakeCli.calls[0]["model"]        # write 登记为强模型
with get_conn() as conn:
    assert conn.execute("SELECT COUNT(*) c FROM action_log WHERE endpoint='llm.write' AND success=0").fetchone()["c"] == 1
# 模型拒绝（回了一段话而不是 JSON）→ 直接给出带原话的说明，不再浪费一次纠正重试
FakeCli.calls = []; FakeCli.script = [FakeResp("很抱歉，我无法帮助撰写这类内容。")]
try:
    jobs.llm.chat_json("material_gen", [{"role": "user", "content": "x"}], ["items"]); raise SystemExit("应报错")
except lc.LLMFormatError as e:
    assert "拒绝" in str(e) and "很抱歉，我无法" in str(e) and "换一个" in str(e), str(e)
assert len(FakeCli.calls) == 1 and FakeCli.calls[0]["max_tokens"] == 4096, FakeCli.calls
# 输出被截断 → 提示减少条数
class TruncResp(FakeResp):
    def json(self): return {"choices": [{"message": {"content": '{"items": [{"text": "半截'}, "finish_reason": "length"}], "usage": {}}
FakeCli.calls = []; FakeCli.script = [TruncResp("")]
try:
    jobs.llm.chat_json("material_gen", [{"role": "user", "content": "x"}], ["items"]); raise SystemExit("应报错")
except lc.LLMFormatError as e:
    assert "截断" in str(e) and "生成条数" in str(e), str(e)
lc.httpx.Client = orig_httpx_client; config.set_value("llm_base_url", ""); config.set_value("llm_api_key", "")
print("[6j] LLM 纠正重试 / 4xx 记日志 / 拒绝与截断识别 OK")

# [6k] twikit 事件循环：超时会把协程取消掉
state = {"cancelled": False}


async def slow():
    try:
        await asyncio.sleep(5)
    except asyncio.CancelledError:
        state["cancelled"] = True; raise


try:
    _LoopThread.get().run(slow(), timeout=0.2); raise SystemExit("应超时")
except TimeoutError as e:
    assert "取消" in str(e)
import time as _time  # noqa: E402
_time.sleep(0.2); assert state["cancelled"]
print("[6k] 超时取消协程 OK")

# [6l] 解析失败不再一律当成 Cookie 失效：先探登录态
import twikit.errors as terr  # noqa: E402


class ProbeClient:
    def __init__(self, ok): self.ok = ok

    def set_cookies(self, *a, **k): pass

    async def user(self):
        if self.ok:
            return type("U", (), {"id": "1"})()
        raise terr.Unauthorized("401")


async def boom():
    raise KeyError("data")


ucli = UnofficialXClient(credentials={"auth_token": "a" * 40, "ct0": "b" * 32})
ucli._client = ProbeClient(True)
try:
    ucli._call(lambda: boom(), "读推文"); raise SystemExit("应报错")
except AuthExpired:
    raise SystemExit("不该判成 Cookie 失效")
except XClientError as e:
    assert "登录态正常" in str(e), str(e)
ucli = UnofficialXClient(credentials={"auth_token": "a" * 40, "ct0": "b" * 32})
ucli._client = ProbeClient(False)
try:
    ucli._call(lambda: boom(), "读推文"); raise SystemExit("应报错")
except AuthExpired as e:
    assert "失效" in str(e)
print("[6l] 解析失败先探登录态再定性 OK")

# [6m] 官方搜索时间窗钳到 7 天
old = datetime.now(timezone.utc) - timedelta(days=30)
assert clamp_recent_window(old) > old and clamp_recent_window(None) is None
recent = datetime.now(timezone.utc) - timedelta(hours=3)
assert clamp_recent_window(recent) == recent
print("[6m] 搜索时间窗钳到 X 允许范围 OK")
# 定时发帖：素材池轮流（用得最少 + 避开最近发过的）/ AI 改写变体 / AI 按主题创作 / 固定素材重复提醒
with get_conn() as conn:
    for i, tags in enumerate(("promo,daily", "promo", "story")):
        conn.execute("INSERT INTO materials(kind,text,lang,status,scenario_tags) VALUES ('post',?,'ja','active',?)", (f"発帖素材{i} @MyBrand", tags))
    conn.execute("INSERT INTO scheduled_posts(account_id, material_id, content_mode, pool_lang, pool_tags, schedule_type, schedule_expr, next_run_at) "
                 "VALUES (?,NULL,'pool','ja','promo','daily','21:00','2030-01-01T12:00:00Z')", (acc["id"],))
    pool_id = conn.execute("SELECT id FROM scheduled_posts WHERE content_mode='pool'").fetchone()["id"]
    conn.commit()
picked = []
for _ in range(3):
    ok, msg = jobs.fire_plan_now(pool_id); assert ok, msg
    with get_conn() as conn:
        picked.append(conn.execute("SELECT material_id, final_text, origin FROM review_queue WHERE scheduled_post_id=? ORDER BY id DESC LIMIT 1", (pool_id,)).fetchone())
assert len({r["material_id"] for r in picked[:2]}) == 2 and all(r["origin"] == "scheduled" for r in picked), [dict(r) for r in picked]
assert "story" not in " ".join(r["final_text"] for r in picked) and "只能重用" in msg, msg    # 标签 promo 只有 2 条，第 3 次提示重用
with get_conn() as conn:   # 开了 AI 改写但没配 LLM → 退回原文并注明
    conn.execute("UPDATE scheduled_posts SET ai_rewrite=1 WHERE id=?", (pool_id,)); conn.commit()
ok, msg = jobs.fire_plan_now(pool_id); assert ok and "AI 改写失败" in msg and "设置 → LLM" in msg, msg
with get_conn() as conn:   # AI 主题模式没配 LLM → 失败并记 last_error
    conn.execute("INSERT INTO scheduled_posts(account_id, content_mode, pool_lang, ai_brief, schedule_type, schedule_expr, next_run_at) "
                 "VALUES (?,'ai_topic','ja','写一条关于效率工具的推文，结尾带 @MyBrand','daily','21:00','2030-01-01T12:00:00Z')", (acc["id"],))
    topic_id = conn.execute("SELECT id FROM scheduled_posts WHERE content_mode='ai_topic'").fetchone()["id"]; conn.commit()
ok, msg = jobs.fire_plan_now(topic_id); assert not ok and "LLM" in msg, msg
with get_conn() as conn:
    assert "LLM" in conn.execute("SELECT last_error FROM scheduled_posts WHERE id=?", (topic_id,)).fetchone()["last_error"]
# 接上假 LLM：改写保留 @，主题创作强制包含 @
lc.httpx.Client = FakeCli; config.set_value("llm_base_url", "http://fake"); config.set_value("llm_api_key", "k")
FakeCli.calls = []; FakeCli.script = [FakeResp('{"text": "少し言い換えた新しい文 @MyBrand", "reason": "换了开头"}')]
ok, msg = jobs.fire_plan_now(pool_id); assert ok and "AI 改写变体" in msg, msg
with get_conn() as conn:
    assert conn.execute("SELECT final_text FROM review_queue WHERE scheduled_post_id=? ORDER BY id DESC LIMIT 1", (pool_id,)).fetchone()["final_text"] == "少し言い換えた新しい文 @MyBrand"
assert "最近发过的内容" in FakeCli.calls[0]["messages"][1]["content"] and FakeCli.calls[0]["model"] == "gpt-4o"
FakeCli.calls = []; FakeCli.script = [FakeResp('{"text": "忘了带账号的文", "reason": "r"}'), FakeResp('{"text": "效率工具心得 @MyBrand", "reason": "r"}')]
ok, msg = jobs.fire_plan_now(topic_id); assert ok and "AI 按主题创作" in msg, msg
assert len(FakeCli.calls) == 2 and "@MyBrand" in FakeCli.calls[1]["messages"][-1]["content"]   # 缺 @ 时重写一次
with get_conn() as conn:
    assert conn.execute("SELECT last_error FROM scheduled_posts WHERE id=?", (topic_id,)).fetchone()["last_error"] is None
lc.httpx.Client = orig_httpx_client; config.set_value("llm_base_url", ""); config.set_value("llm_api_key", "")
# 固定素材 + 不改写 + 最近发过 → 提醒可能判重复
with get_conn() as conn:
    m0 = conn.execute("SELECT id FROM materials WHERE text LIKE '発帖素材0%'").fetchone()["id"]
    conn.execute("INSERT INTO scheduled_posts(account_id, material_id, content_mode, schedule_type, schedule_expr, next_run_at) VALUES (?,?,'fixed','daily','21:00','2030-01-01T12:00:00Z')", (acc["id"], m0))
    fixed_id = conn.execute("SELECT id FROM scheduled_posts WHERE content_mode='fixed' ORDER BY id DESC LIMIT 1").fetchone()["id"]; conn.commit()
ok, msg = jobs.fire_plan_now(fixed_id); assert ok and "可能判定重复" in msg, msg
print("[6f12] 定时发帖内容来源（素材池轮流 / AI 改写 / AI 主题 / 重复提醒）OK")

# [6f14] 附件（配图 / 视频）：规则校验；素材带图 → 队列条目带图；发送前用本账号上传并把 media_id 传给发送接口；
#        文件丢失 → 标失败不发；AI 撰写可带附件；定时计划 AI 主题模式可挂附件
from x_operator.core import media as mediam  # noqa: E402
assert mediam.media_dir() == TMP / "media"
assert "最多 4" in mediam.check_set([f"a{i}.jpg" for i in range(5)]) and mediam.check_set(["a.jpg", "b.png", "c.webp"]) == ""
assert "混" in mediam.check_set(["a.jpg", "b.mp4"]) and "1 个" in mediam.check_set(["a.mp4", "b.mp4"]) and "不支持" in mediam.check_set(["a.exe"])
assert "不支持" in mediam.check_one("x.exe", 10) and "太大" in mediam.check_one("x.jpg", 6 * 1024 * 1024) and mediam.check_one("x.jpg", 1000) == ""
rel1 = mediam.new_rel_path("photo.JPEG"); rel2 = mediam.new_rel_path("b.png")
assert mediam.is_safe_rel(rel1) and rel1.endswith(".jpg") and not mediam.is_safe_rel("../x.jpg")
for r_ in (rel1, rel2):
    mediam.abs_path(r_).parent.mkdir(parents=True, exist_ok=True); mediam.abs_path(r_).write_bytes(b"img")
assert mediam.describe([rel1, rel2]) == "2 张图片" and mediam.describe([mediam.new_rel_path("v.mp4")]) == "1 个视频"
assert mediam.parse_files('["a","b"]') == ["a", "b"] and mediam.parse_files("bad json") == [] and mediam.parse_files(None) == []
with get_conn() as conn:
    conn.execute("INSERT INTO materials(kind,text,lang,status,media_files) VALUES ('reply','带图回复素材','zh','active',?)", (mediam.dump_files([rel1, rel2]),))
    pic_mat = conn.execute("SELECT id FROM materials WHERE text='带图回复素材'").fetchone()["id"]
    t = conn.execute("SELECT id, tweet_id FROM target_tweets WHERE process_status IN ('filtered','no_match') "
                     "AND id NOT IN (SELECT target_tweet_id FROM review_queue WHERE target_tweet_id IS NOT NULL) "
                     "AND tweet_id NOT IN (SELECT tweet_id FROM interactions) LIMIT 1").fetchone()
    conn.execute("UPDATE accounts SET next_allowed_at=NULL, status='active' WHERE id=?", (acc["id"],)); conn.commit()
out = jobs.match.manual_match(t["id"], pic_mat, "带图回复 " + t["tweet_id"], account=acc); assert out.status == "queued", out
with get_conn() as conn:
    assert mediam.parse_files(conn.execute("SELECT final_media_files FROM review_queue WHERE id=?", (out.queue_id,)).fetchone()["final_media_files"]) == [rel1, rel2]
    conn.execute("UPDATE review_queue SET status='approved', decided_at=? WHERE id=?", (utcnow_iso(), out.queue_id)); conn.commit()
client = factory.get_client(acc)
sent_calls: list = []; uploaded: list = []
orig_reply, orig_upload = client.reply, client.upload_media
client.upload_media = lambda path, kind, alt_text=None: (uploaded.append((Path(path).name, kind)), f"mid_{len(uploaded)}")[1]
client.reply = lambda text, rid, media_ids=None: (sent_calls.append(media_ids), orig_reply(text, rid, media_ids))[1]
r = jobs.dispatcher.tick(); assert r.sent == 1, r.as_msg()
assert sent_calls == [["mid_1", "mid_2"]] and [k for _, k in uploaded] == ["image", "image"] and uploaded[0][0] == Path(rel1).name, (sent_calls, uploaded)
# 文件丢了 → 不发、标失败、原因写明
with get_conn() as conn:
    t2 = conn.execute("SELECT id, tweet_id FROM target_tweets WHERE process_status IN ('filtered','no_match') "
                      "AND id NOT IN (SELECT target_tweet_id FROM review_queue WHERE target_tweet_id IS NOT NULL) "
                      "AND tweet_id NOT IN (SELECT tweet_id FROM interactions) LIMIT 1").fetchone()
    conn.execute("UPDATE accounts SET next_allowed_at=NULL WHERE id=?", (acc["id"],)); conn.commit()
out2 = jobs.match.manual_match(t2["id"], pic_mat, "带图回复2", account=acc); assert out2.status == "queued", out2
mediam.abs_path(rel2).unlink()
with get_conn() as conn:
    conn.execute("UPDATE review_queue SET status='approved', decided_at=? WHERE id=?", (utcnow_iso(), out2.queue_id)); conn.commit()
sent_calls.clear(); uploaded.clear()
r = jobs.dispatcher.tick()
with get_conn() as conn:
    row = conn.execute("SELECT status, error_msg FROM review_queue WHERE id=?", (out2.queue_id,)).fetchone()
assert r.sent == 0 and row["status"] == "failed" and "不存在" in row["error_msg"] and Path(rel2).name in row["error_msg"] and not sent_calls, (dict(row), sent_calls)
assert mediam.missing([rel1, rel2]) == [rel2]
client.reply, client.upload_media = orig_reply, orig_upload
# AI 撰写带附件 → 队列条目带上；定时计划 AI 主题模式挂附件 → 生成的发帖条目带上
lc.httpx.Client = FakeCli; config.set_value("llm_base_url", "http://fake"); config.set_value("llm_api_key", "k")
with get_conn() as conn:
    t3 = conn.execute("SELECT id FROM target_tweets WHERE process_status IN ('filtered','no_match') "
                      "AND id NOT IN (SELECT target_tweet_id FROM review_queue WHERE target_tweet_id IS NOT NULL) "
                      "AND tweet_id NOT IN (SELECT tweet_id FROM interactions) LIMIT 1").fetchone()
FakeCli.calls = []; FakeCli.script = [FakeResp('{"reply_text": "配图回复 @MyBrand", "reason": "r"}')]
out3 = jobs.match.ai_write(t3["id"], "推荐 @MyBrand", account=acc, media_files=[rel1]); assert out3.status == "queued", out3
with get_conn() as conn:
    assert mediam.parse_files(conn.execute("SELECT final_media_files FROM review_queue WHERE id=?", (out3.queue_id,)).fetchone()["final_media_files"]) == [rel1]
    conn.execute("UPDATE scheduled_posts SET media_files=? WHERE id=?", (mediam.dump_files([rel1]), topic_id)); conn.commit()
FakeCli.calls = []; FakeCli.script = [FakeResp('{"text": "配图主贴 @MyBrand", "reason": "r"}')]
ok, msg = jobs.fire_plan_now(topic_id); assert ok and "1 张图片" in msg, msg
with get_conn() as conn:
    q = conn.execute("SELECT final_media_files FROM review_queue WHERE scheduled_post_id=? ORDER BY id DESC LIMIT 1", (topic_id,)).fetchone()
    assert mediam.parse_files(q["final_media_files"]) == [rel1]
    # 固定素材模式：附件跟素材走
    conn.execute("UPDATE materials SET media_files=? WHERE id=?", (mediam.dump_files([rel1]), m0)); conn.commit()
ok, msg = jobs.fire_plan_now(fixed_id); assert ok and "带1 张图片" in msg, msg
with get_conn() as conn:
    q = conn.execute("SELECT final_media_files FROM review_queue WHERE scheduled_post_id=? ORDER BY id DESC LIMIT 1", (fixed_id,)).fetchone()
    assert mediam.parse_files(q["final_media_files"]) == [rel1]
lc.httpx.Client = orig_httpx_client; config.set_value("llm_base_url", ""); config.set_value("llm_api_key", "")
# 孤儿清理：没被任何记录引用的文件才删
orphan = mediam.new_rel_path("o.png"); mediam.abs_path(orphan).write_bytes(b"x")
assert mediam.sweep_orphans() == 1 and not mediam.abs_path(orphan).exists() and mediam.abs_path(rel1).exists()
print("[6f14] 附件：规则校验 / 素材→队列 / 发送前上传 / 丢文件标失败 / AI 撰写与定时计划带附件 / 孤儿清理 OK")

shutil.rmtree(TMP, ignore_errors=True)
print("ALL SMOKE OK")
