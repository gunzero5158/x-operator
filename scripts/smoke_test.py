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
c.execute("INSERT INTO accounts(handle, display_name, access_type, is_primary, credentials) VALUES ('apimax_jp','ApiMax','official',1,'{}')")
demo_acc = c.execute("SELECT id FROM accounts WHERE handle='apimax_jp'").fetchone()["id"]
for t in seed.DEMO_MATERIAL_TEXTS:
    c.execute("INSERT INTO materials(kind,text,lang,status) VALUES ('reply',?,'ja','active')", (t,))
c.execute("INSERT INTO watched_users(handle,x_user_id) VALUES ('indie_ai_dev','mock_user_indie_ai_dev')")
c.execute("INSERT INTO search_rules(name,keyword_query,semantic_criteria) VALUES ('AI API 成本痛点',?, 'x')", (seed.DEMO_RULE_QUERY,))
c.execute("INSERT INTO target_tweets(tweet_id,author_id,author_handle,text,tweet_created_at,source,process_status) VALUES ('1','mock_user_a','a','hi','2026-01-01T00:00:00Z','monitor','queued')")
tt = c.execute("SELECT id FROM target_tweets").fetchone()["id"]
c.execute("INSERT INTO review_queue(account_id,action_type,target_tweet_id,final_text,status,created_at) VALUES (?,'reply',?,'x','pending',?)", (demo_acc, tt, utcnow_iso()))
c.execute("INSERT INTO action_log(account_id,api_kind,endpoint,success,created_at) VALUES (?,'x_mock','e',1,?)", (demo_acc, utcnow_iso()))
c.execute("INSERT INTO accounts(handle, access_type, credentials) VALUES ('my_real','unofficial',?)", (json.dumps({"auth_token": "a" * 40, "ct0": "b" * 32}),))
c.execute("INSERT INTO materials(kind,text,lang,status) VALUES ('reply','我的素材','zh','active')")
c.execute("INSERT INTO search_rules(name,keyword_query,semantic_criteria,lang,min_llm_score) VALUES ('我的规则','foo bar','找人','ja,en',7)")
c.execute("INSERT INTO app_settings(key,value) VALUES ('tweet_max_age_hours','48')")
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
assert ver == 5 and accs == ["my_real"] and mats == ["我的素材"] and rules == ["我的规则"] and wu == 0 and tt_n == 0 and rq_n == 0 and dry is None, (ver, accs, mats, rules, wu, tt_n, rq_n, dry)
assert my_min == 5 and obsolete == 0, (my_min, obsolete)
print("[1] v2→v5 升级 OK：演示数据全部清除、用户数据保留；旧默认达标分 7→5；废弃设置键已清")

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
    for lang, text in (("ja", "画像生成のAPIコストで悩んでいるなら、従量課金の選択肢もありますよ"), ("en", "If API pricing is the blocker, PAYG gateways help."), ("zh", "如果卡在 API 成本上，可以试试按量计费")):
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
assert normalize_keywords("(API 料金 OR API コスト) (AI OR LLM)") == "(API 料金 OR API コスト) (AI OR LLM)"
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
out = jobs.match.ai_write(tgt2["id"], "推荐我们的网关 @ApiMaxJP")
assert out.status == "no_match" and "设置 → LLM" in out.reason, out
print("[3k] 无 LLM 时 AI 撰写给出明确提示 OK:", out.reason[:40])
from x_operator.core.matcher import extract_must_include  # noqa: E402
assert extract_must_include("带上 https://apimax.jp/ 和 @ApiMaxJP，谢谢") == ["https://apimax.jp/", "@ApiMaxJP"]
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
obj = jobs.llm.chat_json("t", [{"role": "user", "content": "hi"}], ["ok"])
assert obj == {"ok": True} and len(FakeCli.calls) == 2 and len(FakeCli.calls[1]["messages"]) == 3 and FakeCli.calls[1]["messages"][1]["content"] == "sorry, no json here", FakeCli.calls
FakeCli.calls = []; FakeCli.script = [FakeResp("bad key", status=401)]
try:
    jobs.llm.chat_json("t2", [{"role": "user", "content": "hi"}], ["ok"]); raise SystemExit("应报错")
except lc.LLMError as e:
    assert "401" in str(e)
with get_conn() as conn:
    assert conn.execute("SELECT COUNT(*) c FROM action_log WHERE endpoint='llm.t2' AND success=0").fetchone()["c"] == 1
lc.httpx.Client = orig_httpx_client; config.set_value("llm_base_url", ""); config.set_value("llm_api_key", "")
print("[6j] LLM 纠正重试 / 4xx 记日志 OK")

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

shutil.rmtree(TMP, ignore_errors=True)
print("ALL SMOKE OK")
