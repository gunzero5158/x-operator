"""Deletion races and bounded dispatch; disposable SQLite and fake providers only."""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch
import pytest
from x_operator.db import database
from x_operator.db.database import init_db, get_conn
from x_operator.core.search import SearchJob, ScoredCandidate
from x_operator.core.search_lifecycle import active_rule, SearchCancelled
from x_operator.core.matcher import MatchEngine
from x_operator.core.dispatcher import Dispatcher
from x_operator.core.compliance import ComplianceGuard
from x_operator.adapters.base import TweetData, PermissionDenied
from x_operator.ui.queue import _load, _counts, _where

@pytest.fixture
def db(tmp_path):
    if getattr(database._local, "conn", None):
        database._local.conn.close()
        del database._local.conn
    init_db(tmp_path / "test.db")
    with get_conn() as c:
        c.execute("INSERT INTO accounts(handle,status,access_type,active_hours_start,active_hours_end) VALUES ('fake','active','unofficial','00:00','00:00')")
        for name in ('A','B'):
            c.execute("INSERT INTO search_rules(name,keyword_query,semantic_criteria) VALUES (?,'x','x')",(name,))
        c.commit()
        yield_account = c.execute('SELECT * FROM accounts').fetchone()
    yield yield_account
    database._local.conn.close()
    del database._local.conn

def delete_rules():
    with get_conn() as c:
        c.execute('DELETE FROM search_rules'); c.commit()

def tweet(n=100):
    return TweetData(str(n),'author','author','test','en',datetime.now(timezone.utc),False,None)

def test_delete_during_fetch_cancels_current_and_pending_rules(db):
    pool=SimpleNamespace(pick=lambda:(db,''),wait_gap=lambda:None,note_request=lambda a:None,summary=lambda:'')
    match=Mock()
    job=SearchJob(match,SimpleNamespace(configured=False))
    def fetch(*args,**kwargs):
        delete_rules()
        return [ScoredCandidate(tweet(),10,'test')]
    with patch('x_operator.core.search.ReadPool',return_value=pool), patch.object(job,'run_rule',side_effect=fetch) as run:
        result=job.run_once(auto=True)
        assert run.call_count == 1
        assert result.errors == 0
        assert job.run_once(auto=True).rules_run == 0
    match.run.assert_not_called()
    with get_conn() as c:
        assert c.execute('SELECT COUNT(*) FROM target_tweets').fetchone()[0] == 0
    assert active_rule.get() is None

def test_delete_during_generation_cannot_enqueue_but_manual_still_can(db):
    from x_operator.core.monitor import store_target
    tid=store_target(tweet(),'search',1)
    engine=MatchEngine(Mock())
    token=active_rule.set(1)
    delete_rules()
    try:
        with pytest.raises(SearchCancelled):
            engine._enqueue(1,tid,None,'draft','test',1,'ai_write')
        with pytest.raises(SearchCancelled):
            store_target(tweet(101),'search',1)
    finally:
        active_rule.reset(token)
    with get_conn() as c:
        assert c.execute('SELECT COUNT(*) FROM review_queue').fetchone()[0] == 0
    assert engine._enqueue(1,tid,None,'manual','test',1,'manual')

def tasks(n):
    with get_conn() as c:
        for i in range(n):
            tid=c.execute("INSERT INTO target_tweets(tweet_id,author_id,author_handle,text,source,process_status,tweet_created_at) VALUES (?,'a','a','test','search','queued','2026-10-16T00:00:00Z')",(str(1000+i),)).lastrowid
            c.execute("INSERT INTO review_queue(account_id,action_type,target_tweet_id,final_text,status) VALUES (1,'reply',?,'draft','approved')",(tid,))
        c.commit()

@pytest.mark.parametrize('message,expected',[('target (385)',5),('target (433)',5),('daily limit (501)',1),('unknown permission',1)])
def test_dispatch_stops_at_bound_or_account_error(db,message,expected):
    tasks(7)
    client=Mock();client.reply.side_effect=PermissionDenied(message)
    dispatcher=Dispatcher(ComplianceGuard())
    with patch('x_operator.core.dispatcher.factory.get_client',return_value=client):
        dispatcher.tick()
    assert client.reply.call_count == expected
    with get_conn() as c:
        assert c.execute("SELECT COUNT(*) FROM review_queue WHERE status='failed'").fetchone()[0] == expected

def test_target_failure_then_success_stops(db):
    tasks(4)
    dispatcher=Dispatcher(ComplianceGuard())
    calls=[]
    def send(account,item):
        calls.append(item['id'])
        dispatcher._set_status(item['id'],'failed' if len(calls)==1 else 'sent',error_msg='(385)' if len(calls)==1 else None)
        return len(calls)>1
    with patch.object(dispatcher,'send_item',side_effect=send):
        assert dispatcher.tick().sent == 1
    assert len(calls)==2

def test_error_filter_counts_pages_and_selection_match(db):
    tasks(4)
    with get_conn() as c:
        for i,msg in enumerate(('old wrapper (385)','old wrapper (433)','old wrapper (501)','network'),1):
            c.execute("UPDATE review_queue SET status='failed',error_msg=? WHERE id=?",(msg,i))
        c.commit()
    assert _counts(error_kind='target')['failed']==2
    assert len(_load('failed',limit=1,error_kind='target'))==1
    where,args=_where('failed',error_kind='target')
    with get_conn() as c:
        assert {r[0] for r in c.execute('SELECT id FROM review_queue rq WHERE '+where,args)}=={1,2}
    assert [r['id'] for r in _load('failed',error_kind='quota')]==[3]
    assert [r['id'] for r in _load('failed',error_kind='other')]==[4]


def test_delete_inside_llm_stops_before_more_generation(db):
    from x_operator.core.monitor import store_target
    tid = store_target(tweet(), 'search', 1)
    llm = Mock()
    def write(*args):
        delete_rules()
        return {'reply_text': 'draft', 'confidence': 1}
    llm.write_reply.side_effect = write
    token = active_rule.set(1)
    try:
        with patch('x_operator.core.matcher.textlimit.fit') as fit:
            with pytest.raises(SearchCancelled):
                MatchEngine(llm).ai_write(tid, 'brief', account=db)
            fit.assert_not_called()
    finally:
        active_rule.reset(token)
    with get_conn() as c:
        assert c.execute('SELECT COUNT(*) FROM review_queue').fetchone()[0] == 0
