"""Heavy queue + actual transport interruption regression; isolated mock DB, no scheduler."""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]


def check(baseline=False):
    from playwright.sync_api import sync_playwright, expect
    with tempfile.TemporaryDirectory(prefix='queue-load-') as folder:
        db = Path(folder) / 'test.db'
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        base = f'http://127.0.0.1:{port}'
        with (Path(folder) / 'server.log').open('w', encoding='utf-8') as log:
            proc = subprocess.Popen([sys.executable, str(ROOT / 'scripts/queue_refresh_check.py'),
                                     '--serve', str(db), '--port', str(port)], cwd=ROOT, stdout=log, stderr=log)
            try:
                for _ in range(200):
                    try:
                        with urlopen(base + '/queue-check-fingerprint', timeout=1) as response:
                            identity = json.load(response)
                        break
                    except OSError:
                        if proc.poll() is not None:
                            raise RuntimeError((Path(folder) / 'server.log').read_text(encoding='utf-8'))
                        time.sleep(.1)
                else:
                    raise RuntimeError('Server startup timed out')
                assert identity['mock'] and Path(identity['repository']) == ROOT
                assert identity['queue_sha256'] == hashlib.sha256((ROOT / 'x_operator/ui/queue.py').read_bytes()).hexdigest()
                with closing(sqlite3.connect(db)) as conn:
                    conn.executemany("INSERT INTO review_queue(account_id,action_type,final_text,status) VALUES (1,'post',?,'failed')",
                                     [(f'additional {i}',) for i in range(1802)])
                    conn.execute("UPDATE review_queue SET final_text=?, llm_reason=?, error_msg=? WHERE status='failed'",
                                 ('Large sample draft. 中文测试文案\n' * 100, 'Synthetic explanation ' * 100,
                                  "Authorization: You've hit the daily limit. (501)"))
                    conn.execute("UPDATE target_tweets SET text=?, text_zh=?", ('Source sample ' * 200, '中文翻译示例 ' * 200))
                    conn.commit()
                with sync_playwright() as p:
                    browser = p.chromium.launch(channel='msedge', headless=True)
                    context = browser.new_context(locale='zh-CN')
                    page = context.new_page()
                    errors = []
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.add_init_script("window.longTasks=[]; new PerformanceObserver(l=>window.longTasks.push(...l.getEntries().map(e=>e.duration))).observe({type:'longtask',buffered:true})")
                    start = time.perf_counter()
                    page.goto(base + '/queue?status=failed&account=1&action_type=all')
                    boxes = page.get_by_role('checkbox', name='选择', exact=True)
                    expect(boxes).to_have_count(200 if baseline else 25, timeout=30000)
                    loaded = time.perf_counter() - start
                    start = time.perf_counter()
                    page.get_by_role('button', name='全选列表所有条目', exact=True).click()
                    expect(page.get_by_text('已选 2000 条', exact=True)).to_be_visible(timeout=30000)
                    selected = time.perf_counter() - start
                    metrics = {'baseline': baseline, 'rows': 2000, 'visible': boxes.count(),
                               'load_seconds': round(loaded, 3), 'select_seconds': round(selected, 3),
                               'dom_nodes': page.locator('*').count(),
                               'max_long_task_ms': page.evaluate('Math.max(0,...window.longTasks)')}
                    print(json.dumps(metrics), flush=True)
                    if not baseline:
                        # Pagination must retain exclusions and the full-filter selection snapshot.
                        boxes.first.click()
                        expect(page.get_by_text('已选 1999 条', exact=True)).to_be_visible()
                        page.get_by_role('button', name='下一页', exact=True).first.click()
                        expect(page.get_by_text('第 2 / 80 页 · 共 2000 条', exact=True).first).to_be_visible()
                        expect(page.get_by_text('已选 1999 条', exact=True)).to_be_visible()
                        expect(boxes.first).to_be_checked()
                        page.get_by_role('button', name='上一页', exact=True).first.click()
                        expect(boxes.first).not_to_be_checked()
                        # Keep a live page disconnected long enough to exercise the reload path.
                        context.set_offline(True)
                        old_client = page.evaluate('window.clientId')
                        page.evaluate('window.socket.io.engine.close()')
                        page.wait_for_function('!window.socket.connected')
                        page.wait_for_timeout(12000)
                        context.set_offline(False)
                        page.wait_for_function('old => window.clientId !== old && window.socket?.connected', arg=old_client, timeout=30000)
                        expect(page.locator('.xo-toolbar .q-select').first).to_contain_text('失败')
                        expect(boxes).to_have_count(25)
                        # Change the filter while transport is absent: URL must change locally.
                        context.set_offline(True)
                        page.locator('.xo-toolbar .q-select').nth(2).click()
                        page.get_by_role('option', name='回复', exact=True).click()
                        assert 'action_type=reply' in page.url, page.url
                        context.set_offline(False)
                        page.reload()
                        expect(page.locator('.xo-toolbar .q-select').nth(2)).to_contain_text('回复')
                        page.get_by_role('button', name='全选列表所有条目', exact=True).click()
                        expect(page.get_by_text('已选 99 条', exact=True)).to_be_visible()
                        assert not errors, errors
                        assert selected < 5, metrics
                        assert metrics['dom_nodes'] < 12000, metrics
                        print('PASS: heavy queue, paged snapshots, disconnect and offline-filter reload', flush=True)
                    browser.close()
            finally:
                try:
                    with urlopen(Request(base + '/queue-check-shutdown', method='POST'), timeout=5):
                        pass
                    proc.wait(timeout=15)
                except Exception:
                    subprocess.run(['taskkill', '/PID', str(proc.pid), '/T', '/F'], capture_output=True)
                    proc.wait(timeout=10)
                if proc.returncode not in (0, None):
                    print((Path(folder) / 'server.log').read_text(encoding='utf-8')[-4000:])


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--baseline', action='store_true')
    check(parser.parse_args().baseline)
