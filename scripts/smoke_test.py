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
    age = conn.execute("SELECT value FROM app_settings WHERE key='tweet_max_age_hours'").fetchone()["value"]
assert ver == 5 and accs == ["my_real"] and mats == ["我的素材"] and rules == ["我的规则"] and wu == 0 and tt_n == 0 and rq_n == 0 and dry is None, (ver, accs, mats, rules, wu, tt_n, rq_n, dry)
assert my_min == 5 and age == "168", (my_min, age)
print("[1] v2→v5 升级 OK：演示数据全部清除、用户数据保留；旧默认阈值放宽（达标分 7→5，最大年龄 48→168h）")

# ---------- 2. 全新库：干干净净 ----------
fresh_conn_state()
init_db(TMP / 'new.db')
with get_conn() as conn:
    for tbl in ("accounts", "materials", "watched_users", "search_rules", "target_tweets"):
        assert conn.execute(f"SELECT COUNT(*) c FROM {tbl}").fetchone()["c"] == 0, tbl
    assert conn.execute("SELECT COUNT(*) c FROM app_settings WHERE key='dry_run'").fetchone()["c"] == 0
print("[2] 新库无任何演示数据 OK")

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
os.environ["HTTPS_PROXY"] = "https://127.0.0.1:7890"; os.environ.pop("HTTP_PROXY", None); os.environ.pop("ALL_PROXY", None)
assert detect_system_proxy() == "http://127.0.0.1:7890", detect_system_proxy()
assert resolve_proxy({}) == "http://127.0.0.1:7890" and resolve_proxy({"proxy": "direct"}) is None and resolve_proxy({"proxy": "10.0.0.1:1080"}) == "http://10.0.0.1:1080"
os.environ.pop("HTTPS_PROXY"); assert detect_system_proxy() is None
print("[5a] 系统代理检测/直连/自定义 OK")
assert validate_unofficial_credentials({"auth_token": "abc", "ct0": "d" * 32}).startswith("auth_token 格式不对")
assert validate_unofficial_credentials({"auth_token": "a" * 40, "ct0": "zz"}).startswith("ct0 格式不对")
assert validate_unofficial_credentials({"auth_token": "a" * 40}).startswith("auth_token 和 ct0 要一起填")
assert "6 位数字" in validate_unofficial_credentials({"totp_secret": "123456"})
assert validate_unofficial_credentials({"auth_token": "a" * 40, "ct0": "b" * 160, "totp_secret": "jbsw y3dp ehpk 3pxp"}) == ""
print("[5b] 凭据格式校验 OK")
oc = OfficialXClient(credentials={"consumer_key": "k", "consumer_secret": "s", "access_token": "t", "access_token_secret": "ts", "proxy": "127.0.0.1:9999"})
assert oc._client.session.proxies["https"] == "http://127.0.0.1:9999" and oc.proxy_used == "http://127.0.0.1:9999"
print("[5c] 官方 API 客户端代理注入 OK")
shutil.rmtree(TMP, ignore_errors=True)
print("ALL SMOKE OK")
