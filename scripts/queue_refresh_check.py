"""Browser regression for queue refresh; temporary DB, paused mock accounts, no scheduler."""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def serve(db: Path, port: int) -> None:
    os.environ['X_OPERATOR_MOCK'] = '1'
    os.environ['NICEGUI_STORAGE_PATH'] = str(db.parent / 'session')
    from nicegui import app, ui
    from x_operator.core.scheduler import Jobs
    from x_operator.db.database import init_db, get_conn
    from x_operator.ui import queue, targets, settings_page
    init_db(db)
    with get_conn() as conn:
        conn.executemany("INSERT INTO accounts(handle,status,access_type) VALUES (?,'paused','unofficial')", [('mock-one',), ('mock-two',)])
        conn.executemany("INSERT INTO review_queue(account_id,action_type,final_text,status) VALUES (1,'post',?,'failed')", [(f'mock failed {i}',) for i in range(198)])
        for i in range(1, 100):
            tid = conn.execute("INSERT INTO target_tweets(tweet_id,author_id,author_handle,text,process_status,source,tweet_created_at) VALUES (?,?,?,'mock target','queued','search','2026-10-08T00:00:00Z')", (str(i), str(i), f'mock-author-{i}')).lastrowid
            conn.execute("UPDATE review_queue SET action_type='reply',target_tweet_id=? WHERE id=?", (tid, i))
        conn.execute("INSERT INTO review_queue(account_id,action_type,final_text,status) VALUES (2,'post','mock pending','pending')")
        conn.commit()
    fingerprint = {'repository': str(ROOT), 'branch': subprocess.check_output(['git', 'branch', '--show-current'], cwd=ROOT, text=True).strip(),
                   'commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
                   'queue_sha256': hashlib.sha256((ROOT / 'x_operator/ui/queue.py').read_bytes()).hexdigest(), 'mock': True, 'database': str(db)}
    @app.get('/queue-check-fingerprint')
    def identity():
        return fingerprint
    @app.post('/queue-check-shutdown')
    def shutdown():
        # Gracefully stop the disposable server and close all SQLite handles.
        app.shutdown()
        return {'stopping': True}
    jobs = Jobs()
    queue.register(jobs)
    targets.register(jobs)
    settings_page.register(jobs)
    ui.run(host='127.0.0.1', port=port, show=False, reload=False, storage_secret='disposable-queue-check')


def check(baseline: bool) -> None:
    from playwright.sync_api import sync_playwright, expect
    with tempfile.TemporaryDirectory(prefix='x-operator-queue-check-') as folder:
        db = Path(folder) / 'queue.db'
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        with (Path(folder) / 'server.log').open('w', encoding='utf-8') as log:
            process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--serve', str(db), '--port', str(port)], cwd=ROOT, stdout=log, stderr=log)
            try:
                base = f'http://127.0.0.1:{port}'
                for _ in range(100):
                    if process.poll() is not None:
                        raise RuntimeError((Path(folder) / 'server.log').read_text(encoding='utf-8'))
                    try:
                        with urlopen(base + '/queue-check-fingerprint', timeout=1) as response:
                            fingerprint = json.load(response)
                        break
                    except OSError:
                        time.sleep(.1)
                else:
                    raise RuntimeError('Mock server did not start')
                assert fingerprint['mock'] and Path(fingerprint['repository']) == ROOT
                assert fingerprint['queue_sha256'] == hashlib.sha256((ROOT / 'x_operator/ui/queue.py').read_bytes()).hexdigest()
                print('Mock server provenance:', json.dumps(fingerprint), flush=True)
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(channel='msedge', headless=True)
                    page = browser.new_page(locale='zh-CN')
                    errors = []
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.goto(base + '/queue')
                    status = page.locator('.xo-toolbar .q-select').nth(0)
                    status.click()
                    page.get_by_role('option', name='失败（198）', exact=True).click()
                    expect(page.get_by_role('checkbox', name='选择', exact=True)).to_have_count(198, timeout=15000)
                    page.get_by_role('button', name='全选当前列表', exact=True).click()
                    expect(page.get_by_text('已选 198 条', exact=True)).to_be_visible(timeout=15000)
                    first = page.get_by_role('checkbox', name='选择', exact=True).first
                    first.evaluate("el => el.dataset.refreshProbe = 'original'")
                    with closing(sqlite3.connect(db)) as conn:
                        conn.execute("INSERT INTO review_queue(account_id,action_type,final_text,status) VALUES (1,'post','new background mock','failed')")
                        conn.commit()
                    page.wait_for_timeout(6000)
                    retained = first.get_attribute('data-refresh-probe') == 'original'
                    print('After automatic refresh:', {'url': page.url, 'selected198': page.get_by_text('已选 198 条', exact=True).count(), 'original_card_retained': retained}, flush=True)
                    if not baseline:
                        assert retained
                        expect(page).to_have_url(base + '/queue?status=failed&account=0&action_type=all')
                        expect(page.get_by_text('已选 198 条', exact=True)).to_be_visible()
                        page.get_by_role('button', name='取消全选', exact=True).click()
                        expect(page.get_by_text('已选 0 条', exact=True)).to_be_visible()
                        expect(page.get_by_role('checkbox', name='选择', exact=True)).to_have_count(199, timeout=10000)
                        account = page.locator('.xo-toolbar .q-select').nth(1)
                        account.click()
                        page.get_by_role('option', name='@mock-one', exact=False).click()
                        expect(page).to_have_url(base + '/queue?status=failed&account=1&action_type=all')
                    page.reload()
                    expected_count = 1 if baseline else 199
                    expect(page.get_by_role('checkbox', name='选择', exact=True)).to_have_count(expected_count, timeout=15000)
                    print('After reload:', {'url': page.url, 'visible_tasks': expected_count, 'errors': errors}, flush=True)
                    if not baseline:
                        assert not errors
                        page.get_by_role('button', name='全选当前列表', exact=True).click()
                        page.get_by_role('button', name='批量删除', exact=True).click()
                        expect(page.get_by_text('删除选中的 199 条任务？', exact=True)).to_be_visible()
                        page.wait_for_timeout(6000)
                        expect(page.get_by_text('删除选中的 199 条任务？', exact=True)).to_be_visible()
                        page.get_by_role('button', name='取消', exact=True).click()
                        expect(page.get_by_text('已选 199 条', exact=True)).to_be_visible()
                        with closing(sqlite3.connect(db)) as conn:
                            assert conn.execute('SELECT COUNT(*) FROM review_queue').fetchone()[0] == 200
                        kind = page.locator('.xo-toolbar .q-select').nth(2)
                        kind.click()
                        page.get_by_role('option', name='回复', exact=True).click()
                        expect(page.get_by_role('checkbox', name='选择', exact=True)).to_have_count(99)
                        expect(page).to_have_url(base + '/queue?status=failed&account=1&action_type=reply')
                        page.reload()
                        expect(page.get_by_role('checkbox', name='选择', exact=True)).to_have_count(99)
                        page.get_by_role('button', name='全选当前列表', exact=True).click()
                        output = ROOT / 'data/checks'
                        output.mkdir(parents=True, exist_ok=True)
                        for width, height, name in [(1440, 900, 'desktop'), (375, 812, 'mobile'), (812, 375, 'landscape')]:
                            page.set_viewport_size({'width': width, 'height': height})
                            page.screenshot(path=str(output / f'queue-batch-{name}.png'))
                            assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth + 1'), name
                        page.set_viewport_size({'width': 1440, 'height': 900})
                        page.get_by_role('button', name='执行批量操作', exact=True).click()
                        expect(page.get_by_text('对选中的 99 条任务执行「捞回待审核」？', exact=True)).to_be_visible()
                        page.get_by_role('button', name='确认执行', exact=True).click()
                        expect(page.get_by_text('已完成 99 条，未完成 0 条', exact=True)).to_be_visible(timeout=15000)
                        expect(page.get_by_role('checkbox', name='选择', exact=True)).to_have_count(0)
                        with closing(sqlite3.connect(db)) as conn:
                            assert conn.execute("SELECT COUNT(*) FROM review_queue WHERE status='pending' AND action_type='reply'").fetchone()[0] == 99
                            assert conn.execute("SELECT COUNT(*) FROM review_queue WHERE status='failed' AND action_type='post'").fetchone()[0] == 100
                            assert conn.execute('SELECT COUNT(*) FROM review_queue').fetchone()[0] == 200
                        with closing(sqlite3.connect(db)) as conn:
                            conn.executemany("INSERT INTO review_queue(account_id,action_type,final_text,status) VALUES (1,'post',?,'failed')", [(f'overflow {i}',) for i in range(105)])
                            conn.commit()
                        page.goto(base + '/queue?status=failed&account=1&action_type=post')
                        expect(page.get_by_role('checkbox', name='选择', exact=True)).to_have_count(200)
                        expect(page.get_by_role('button', name='执行批量操作', exact=True)).to_be_disabled()
                        expect(page.locator('.xo-toolbar button')).to_have_count(1)  # only dispatcher trigger
                        expect(page.get_by_role('button', name='批量转给其他账号', exact=True)).to_have_count(0)
                        scope = page.locator('.xo-batch-bar .q-select').first
                        scope.click()
                        page.get_by_role('option', name='当前筛选全部', exact=True).click()
                        expect(page.get_by_text('当前筛选共 205 条', exact=True)).to_be_visible()
                        page.get_by_role('button', name='批量删除', exact=True).click()
                        expect(page.get_by_text('删除@mock-one 的「失败 · 发帖」全部 205 条条目？', exact=True)).to_be_visible()
                        page.get_by_role('button', name='取消', exact=True).click()
                        with closing(sqlite3.connect(db)) as conn:
                            assert conn.execute("SELECT COUNT(*) FROM review_queue WHERE status='failed'").fetchone()[0] == 205
                        page.get_by_role('button', name='执行批量操作', exact=True).click()
                        expect(page.get_by_text('对当前筛选全部 205 条任务执行「捞回待审核」？', exact=True)).to_be_visible()
                        page.get_by_role('button', name='确认执行', exact=True).click()
                        expect(page.get_by_text('已完成 205 条，未完成 0 条', exact=True)).to_be_visible(timeout=15000)
                        with closing(sqlite3.connect(db)) as conn:
                            assert conn.execute("SELECT COUNT(*) FROM review_queue WHERE status='failed'").fetchone()[0] == 0
                            assert conn.execute("SELECT COUNT(*) FROM review_queue WHERE status='pending' AND account_id=2").fetchone()[0] == 1
                        page.goto(base + '/targets?status=queued')
                        expect(page.locator('.xo-batch-bar')).to_have_count(1)
                        expect(page.locator('.xo-batch-bar').get_by_role('button', name='全局清理', exact=True)).to_be_visible()
                        expect(page.locator('.xo-batch-bar').get_by_role('button', name='批量删除', exact=True)).to_be_visible()
                        page.goto(base + '/targets?status=filtered')
                        expect(page.locator('.xo-batch-bar').get_by_role('button', name='全局清理', exact=True)).to_be_visible()
                        assert not errors, errors
                        with closing(sqlite3.connect(db)) as conn:
                            cookies = {'auth_token': 'a' * 40, 'ct0': 'b' * 32}
                            conn.execute('UPDATE accounts SET credentials=? WHERE id=1', (json.dumps(cookies),))
                            conn.execute('UPDATE accounts SET credentials=? WHERE id=2', (json.dumps(dict(cookies, username='mock-two', password='mock-password')),))
                            conn.commit()
                        page.goto(base + '/settings')
                        expect(page.get_by_role('button', name='配置浏览器登录', exact=True)).to_have_count(1)
                        expect(page.get_by_role('button', name='浏览器登录（需安装）', exact=True)).to_have_count(1)
                        page.get_by_role('button', name='配置浏览器登录', exact=True).click()
                        expect(page.get_by_text('方式二：账号密码 + 两步验证密钥', exact=True)).to_be_visible()
                        expect(page.get_by_text('方式一：浏览器 Cookie', exact=True)).not_to_be_visible()
                        expect(page.get_by_role('button', name='保存并登录', exact=True)).to_be_visible()
                        for width, height, name in [(1440, 900, 'desktop'), (375, 812, 'mobile')]:
                            page.set_viewport_size({'width': width, 'height': height})
                            page.wait_for_timeout(350)  # Let the dialog transition settle before visual inspection.
                            page.screenshot(path=str(output / f'login-setup-{name}.png'))
                            assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth + 1')
                        page.get_by_role('button', name='取消', exact=True).click()
                        page.set_viewport_size({'width': 1440, 'height': 900})
                        page.get_by_role('button', name='浏览器登录（需安装）', exact=True).click()
                        expect(page.get_by_text('浏览器登录需要专用 Chromium，首次下载一次，所有账号共用。已有 Cookie 的正常使用不需要安装；下载完成后点击「开始登录」。', exact=True)).to_be_visible()
                        page.wait_for_timeout(350)
                        page.screenshot(path=str(output / 'login-install-desktop.png'))
                        with closing(sqlite3.connect(db)) as conn:
                            assert json.loads(conn.execute('SELECT credentials FROM accounts WHERE id=1').fetchone()[0]) == cookies
                        assert not errors, errors
                        print('PASS: login setup opens password form, cookies unchanged on cancel, shared browser installation explained', flush=True)
                        print('PASS: unified batch area, full-filter restore beyond 200, deletion cancel, targets cleanup including empty filter', flush=True)
                        print('PASS: 198-task refresh, type/account filters and reload, confirmation cancel, 99 replies restored; 100 failed posts untouched', flush=True)
                    browser.close()
            finally:
                try:
                    with urlopen(Request(base + '/queue-check-shutdown', method='POST'), timeout=3):
                        pass
                    process.wait(timeout=10)
                except (OSError, subprocess.TimeoutExpired):
                    process.terminate()
                    process.wait(timeout=10)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--serve', type=Path)
    parser.add_argument('--port', type=int)
    parser.add_argument('--baseline', action='store_true')
    args = parser.parse_args()
    if args.serve:
        serve(args.serve, args.port)
    else:
        check(args.baseline)
