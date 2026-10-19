"""Exercise the real PowerShell launcher in an isolated copy with an empty DB."""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import re
import urllib.request

ROOT = Path(__file__).resolve().parents[1]

def run(root, *args, success=True):
    # File handles avoid inherited pipe EOF waiting on the detached server.
    with tempfile.TemporaryFile(mode='w+b') as output:
        result = subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
                                 str(root / 'scripts/restart-local.ps1'), *args], cwd=root,
                                env={**os.environ, 'X_OPERATOR_MOCK': '1'}, stdout=output, stderr=output, timeout=95)
        output.seek(0)
        text = output.read().decode('utf-8', errors='replace')
    print(text.strip(), flush=True)
    assert (result.returncode == 0) == success, text
    return result

with tempfile.TemporaryDirectory(prefix='xop-restart-') as directory:
    root = Path(directory)
    (root / 'scripts').mkdir()
    shutil.copy2(ROOT / 'scripts/restart-local.ps1', root / 'scripts')
    shutil.copytree(ROOT / 'x_operator', root / 'x_operator', ignore=shutil.ignore_patterns('__pycache__'))
    (root / '.venv/Scripts').mkdir(parents=True)
    (root / '.venv/Lib').mkdir()
    shutil.copy2(ROOT / '.venv/Scripts/python.exe', root / '.venv/Scripts')
    shutil.copy2(ROOT / '.venv/pyvenv.cfg', root / '.venv')
    # A junction only supplies installed dependencies, never production data/config.
    junction = root / '.venv/Lib/site-packages'
    subprocess.run(['cmd', '/c', 'mklink', '/J', str(junction), str(ROOT / '.venv/Lib/site-packages')], check=True, capture_output=True)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0)); port = sock.getsockname()[1]
    (root / 'config').mkdir()
    (root / 'config/settings.toml').write_text(f'[app]\nhost="127.0.0.1"\nport={port}\n[defaults]\nauto_jobs_enabled=false\n', encoding='utf-8')
    runtime = root / 'data/local-runtime.json'
    known = {}
    try:
        run(root, '-CheckOnly')
        run(root, '-StartOnly')
        first = json.loads(runtime.read_text(encoding='utf-8-sig')); known[first['pid']] = first['started_at']
        with urllib.request.urlopen(first['url'] + '/healthz') as response:
            health = json.load(response)
        assert health['repository'] == str(root) and health['version'] == re.search(r'__version__ = "([^"]+)"', (ROOT / 'x_operator/version.py').read_text(encoding='utf-8'))[1]
        run(root, '-StartOnly')
        assert json.loads(runtime.read_text(encoding='utf-8-sig'))['pid'] == first['pid']
        runtime.write_text('broken stale record', encoding='utf-8')
        run(root)
        second = json.loads(runtime.read_text(encoding='utf-8-sig')); known[second['pid']] = second['started_at']
        assert second['pid'] != first['pid'] and second['status'] == 'ready'
        run(root, '-CheckOnly')
        (root / 'x_operator/main.py').write_text("raise RuntimeError('fixture startup failure')", encoding='utf-8')
        run(root, success=False)
        assert 'fixture startup failure' in Path(json.loads(runtime.read_text(encoding='utf-8-sig'))['stderr']).read_text(encoding='utf-8')
        # A live port owned by an unrelated process must not be terminated.
        with socket.socket() as foreign:
            foreign.bind(('127.0.0.1', port)); foreign.listen()
            run(root, success=False)
            assert foreign.getsockname()[1] == port
        print('PASS: start, duplicate start, stale-record restart, health, startup failure, foreign port protection')
    finally:
        # Only terminate the fixture processes identified by our own launcher.
        if runtime.exists():
            try:
                record=json.loads(runtime.read_text(encoding='utf-8-sig')); known[record['pid']] = record['started_at']
            except (ValueError, KeyError): pass
        for pid, started in known.items():
            executable = str(root / '.venv/Scripts/python.exe').replace("'", "''")
            command = (f"$p=Get-CimInstance Win32_Process -Filter 'ProcessId={pid}'; "
                       f"if ($p -and $p.ExecutablePath -eq '{executable}' -and "
                       f"[math]::Abs(($p.CreationDate.ToUniversalTime()-[datetimeoffset]::Parse('{started}').UtcDateTime).TotalSeconds) -lt 1) "
                       f"{{ taskkill.exe /PID {pid} /T /F }}")
            subprocess.run(['powershell.exe','-NoProfile','-Command',command], capture_output=True)
        junction.rmdir()
