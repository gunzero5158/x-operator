"""按需安装 Chromium、串行登录队列、批量凭据导入。服务重启后任务不自动续跑。"""
from __future__ import annotations

import atexit
import csv
import importlib.util
import io
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from ..adapters import factory
from ..adapters.real import detect_system_proxy, normalize_totp_secret, parse_credentials, resolve_proxy, validate_unofficial_credentials
from ..db import database
from ..db.database import get_conn, utcnow_iso

ROOT = Path(__file__).resolve().parents[2]
TERMINAL = {"success", "failed", "skipped", "cancelled"}
_lock = threading.RLock()
_jobs: dict[str, "LoginJob"] = {}
_runner: threading.Thread | None = None
_process: subprocess.Popen | None = None
_install_process: subprocess.Popen | None = None
_shutdown = threading.Event()
_installation = {"status": "idle", "message": "尚未下载", "percent": None}


def browser_dir() -> Path:
    if database._DB_PATH is None:
        raise RuntimeError("数据库尚未初始化")
    return database._DB_PATH.resolve().parent / "browser-login" / "chromium"


def environment() -> dict:
    env = os.environ.copy()
    env.update(PLAYWRIGHT_BROWSERS_PATH=str(browser_dir()), PYTHONUTF8="1", PYTHONUNBUFFERED="1")
    # 不让调试环境变量把协议里的输入值打印到日志。
    env.pop("DEBUG", None)
    env.pop("PWDEBUG", None)
    return env


def installed() -> bool:
    """只检查当前 Playwright 版本所需的 Chromium；不启动浏览器或下载。"""
    spec = importlib.util.find_spec("playwright")
    if not spec or not spec.origin:
        return False
    manifest = Path(spec.origin).parent / "driver" / "package" / "browsers.json"
    try:
        info = next(b for b in json.loads(manifest.read_text("utf-8"))["browsers"] if b["name"] == "chromium")
        revisions = {info["revision"], *info.get("revisionOverrides", {}).values()}
        for revision in revisions:
            base = browser_dir() / f"chromium-{revision}"
            for suffix in ("chrome-win64/chrome.exe", "chrome-win/chrome.exe", "chrome-linux64/chrome", "chrome-linux/chrome", "chrome-linux-arm64/chrome",
                           "chrome-mac/Chromium.app/Contents/MacOS/Chromium", "chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
                           "chrome-mac-x64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"):
                if (base / suffix).is_file() and (base / "INSTALLATION_COMPLETE").exists():
                    return True
    except (OSError, ValueError, KeyError, StopIteration):
        pass
    return False


def installation_status() -> dict:
    with _lock:
        result = dict(_installation)
    result["installed"] = installed()
    return result


def _popen(args: list[str], **kwargs) -> subprocess.Popen:
    return subprocess.Popen(args, cwd=ROOT, env=environment(),
                            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
                            start_new_session=sys.platform != "win32", **kwargs)


def _stop_process(proc) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True,
                           creationflags=subprocess.CREATE_NO_WINDOW, timeout=10)
        else:
            os.killpg(proc.pid, signal.SIGTERM)
    except (OSError, subprocess.TimeoutExpired):
        proc.kill()


def install_browser() -> bool:
    """只能由用户的下载按钮触发。不会因保存账号或启动服务而下载安装。"""
    with _lock:
        if _installation["status"] == "downloading":
            return False
        if _process is not None or any(i.status not in TERMINAL for j in _jobs.values() for i in j.items):
            raise ValueError("请先完成或取消登录任务，再重新下载浏览器组件")
        _installation.update(status="downloading", message="正在下载专用 Chromium…", percent=None)
    threading.Thread(target=_install, name="chromium-install", daemon=True).start()
    return True


def _install() -> None:
    global _install_process
    timer = None
    proc = None
    try:
        env = environment()
        proxy = detect_system_proxy()
        if proxy:
            env["HTTPS_PROXY"] = proxy
        env["PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT"] = "120000"
        env["CI"] = "1"
        with _lock:
            if _shutdown.is_set():
                return
            proc = subprocess.Popen([sys.executable, "-m", "playwright", "install", "chromium", "--no-shell"],
                                    cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, encoding="utf-8", errors="replace",
                                    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
                                    start_new_session=sys.platform != "win32")
            _install_process = proc
        timer = threading.Timer(1200, _stop_process, args=(proc,))
        timer.start()
        line = ""
        while char := proc.stdout.read(1):
            line = (line + char)[-1000:]
            if char in "\r\n":
                match = re.search(r"(\d{1,3})%", line)
                with _lock:
                    if match:
                        _installation.update(percent=min(100, int(match[1])), message="正在下载浏览器组件…")
                    elif "downloaded" in line.lower():
                        _installation.update(percent=None, message="正在准备浏览器组件…")
                line = ""
        ok = proc.wait() == 0 and installed()
        with _lock:
            _installation.update(status="ready" if ok else "failed", percent=100 if ok else None,
                                 message="专用 Chromium 已就绪" if ok else "下载未完成，请检查网络、系统代理或磁盘空间后重试")
    except Exception:
        with _lock:
            _installation.update(status="failed", percent=None, message="安装未完成，请同步项目依赖后重试")
    finally:
        if timer:
            timer.cancel()
        _stop_process(proc)
        if proc:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        if proc and proc.stdout:
            proc.stdout.close()
        with _lock:
            _install_process = None


@dataclass
class LoginItem:
    account_id: int
    handle: str
    status: str = "queued"
    message: str = "等待登录"


@dataclass
class LoginJob:
    items: list[LoginItem]
    id: str = field(default_factory=lambda: uuid4().hex)
    created: float = field(default_factory=time.time)
    cancelled: bool = False


def snapshot() -> list[dict]:
    with _lock:
        return [{"id": j.id, "created": j.created, "items": [vars(i).copy() for i in j.items],
                 "finished": all(i.status in TERMINAL for i in j.items)} for j in reversed(list(_jobs.values()))]


def enqueue(account_ids: list[int]) -> str:
    global _runner
    if not installed():
        raise ValueError("请先下载专用 Chromium，再开始登录")
    ids = list(dict.fromkeys(account_ids))
    if not ids or len(ids) > 200:
        raise ValueError("每批请选择 1–200 个账号")
    with _lock:
        if _installation["status"] == "downloading":
            raise ValueError("浏览器组件正在下载，请完成后再登录")
        busy = {i.account_id for j in _jobs.values() for i in j.items if i.status not in TERMINAL}
        items = []
        with get_conn() as conn:
            for aid in ids:
                if aid in busy:
                    continue
                row = conn.execute("SELECT * FROM accounts WHERE id=? AND deleted_at IS NULL", (aid,)).fetchone()
                if not row or row["is_primary"] or row["access_type"] != "unofficial":
                    raise ValueError("浏览器登录只适用于未删除的非官方账号")
                creds = parse_credentials(row["credentials"])
                if not creds.get("username") or not creds.get("password"):
                    raise ValueError(f"@{row['handle']} 还没有填写用户名和密码")
                items.append(LoginItem(aid, row["handle"]))
        if not items:
            raise ValueError("这些账号已经在登录队列中，可打开登录任务查看")
        # 只保留最近 20 批完成的记录，不清除进行中的任务。
        completed = [key for key, j in _jobs.items() if all(i.status in TERMINAL for i in j.items)]
        for key in completed[:-19]:
            del _jobs[key]
        job = LoginJob(items)
        _jobs[job.id] = job
        if _runner is None or not _runner.is_alive():
            _shutdown.clear()
            _runner = threading.Thread(target=_run_queue, name="browser-login-queue", daemon=True)
            _runner.start()
        return job.id


def cancel(job_id: str, account_id: int | None = None) -> None:
    proc = None
    with _lock:
        job = _jobs.get(job_id)
        if not job:
            return
        if account_id is None:
            job.cancelled = True
        for item in job.items:
            if item.status in TERMINAL or (account_id is not None and item.account_id != account_id):
                continue
            if item.status in {"running", "manual"}:
                proc = _process
            item.status = "cancelled" if account_id is None else "skipped"
            item.message = "已取消" if account_id is None else "已跳过，可稍后重试"
    # 不让 taskkill 阻塞页面事件循环。
    if proc:
        threading.Thread(target=_stop_process, args=(proc,), daemon=True).start()


def _update(item: LoginItem, status: str, message: str) -> None:
    with _lock:
        if item.status not in TERMINAL:
            item.status, item.message = status, message


def _save_session(item: LoginItem, original, event: dict) -> None:
    cookies = event.get("cookies", {})
    if event.get("handle", "").lower() != original["handle"].lower() or not cookies.get("auth_token") or not cookies.get("ct0"):
        raise ValueError("登录账号核对失败，未保存 Cookie")
    with _lock:
        if item.status in TERMINAL:
            return
        with get_conn() as conn:
            creds = parse_credentials(original["credentials"])
            creds.update({k: cookies[k] for k in ("auth_token", "ct0")})
            # CAS：登录期间编辑、删除或切换账号后，不把旧任务的 Cookie 写回去。
            result = conn.execute(
                "UPDATE accounts SET credentials=?, status=CASE WHEN status='auth_error' THEN 'active' ELSE status END "
                "WHERE id=? AND handle=? AND credentials=? AND deleted_at IS NULL AND access_type='unofficial' AND is_primary=0",
                (json.dumps(creds, ensure_ascii=False), item.account_id, original["handle"], original["credentials"]))
            conn.commit()
            if result.rowcount != 1:
                raise ValueError("账号在登录期间已被编辑或删除，未覆盖最新设置，请重新登录")
        factory.invalidate(item.account_id)
        item.status, item.message = "success", "网页登录成功，已核对账号并保存 Cookie"


def _run_item(item: LoginItem) -> None:
    global _process
    proc = timer = None
    try:
        with get_conn() as conn:
            original = conn.execute("SELECT * FROM accounts WHERE id=? AND deleted_at IS NULL", (item.account_id,)).fetchone()
        if not original or original["access_type"] != "unofficial" or original["is_primary"]:
            raise ValueError("账号已删除或通道已修改")
        creds = parse_credentials(original["credentials"])
        if not creds.get("username") or not creds.get("password"):
            raise ValueError("账号密码不完整，请编辑后重试")
        creds["totp_secret"] = normalize_totp_secret(creds.get("totp_secret"))
        if validate_unofficial_credentials({"totp_secret": creds["totp_secret"]}):
            raise ValueError("TOTP 密钥格式不正确，请编辑账号后重试")
        with _lock:
            if item.status in TERMINAL or _shutdown.is_set():
                return
            proc = _popen([sys.executable, "-m", "x_operator.core.browser_login_worker"],
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          text=True, encoding="utf-8")
            _process = proc
        timer = threading.Timer(720, _stop_process, args=(proc,))
        timer.start()
        proc.stdin.write(json.dumps({"handle": original["handle"],
                                    "credentials": {k: creds.get(k, "") for k in ("username", "password", "totp_secret", "email")},
                                    "proxy": resolve_proxy(creds), "direct": creds.get("proxy", "").strip().lower() in {"direct", "none", "no", "直连"}}) + "\n")
        proc.stdin.close()
        for line in proc.stdout:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            kind = event.get("kind")
            if kind == "success":
                _save_session(item, original, event)
            elif kind == "error":
                _update(item, "failed", event["message"])
            elif kind in {"progress", "manual"}:
                _update(item, "manual" if kind == "manual" else "running", event["message"])
        proc.wait(timeout=10)
        _update(item, "failed", "登录进程未完成，请重试或改用 Cookie 登录")
    except ValueError as exc:
        _update(item, "failed", str(exc))
    except Exception:
        _update(item, "failed", "登录进程异常结束，请检查浏览器组件、代理及本机桌面环境")
    finally:
        if timer:
            timer.cancel()
        _stop_process(proc)
        if proc:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        if proc and proc.stdout:
            proc.stdout.close()
        with _lock:
            _process = None


def _run_queue() -> None:
    global _runner
    while not _shutdown.is_set():
        with _lock:
            item = next((i for j in _jobs.values() for i in j.items if i.status == "queued"), None)
            if item is None:
                _runner = None
                return
            item.status, item.message = "running", "正在启动专用浏览器"
        _run_item(item)


def shutdown() -> None:
    _shutdown.set()
    with _lock:
        procs = (_process, _install_process)
        for job in _jobs.values():
            for item in job.items:
                if item.status not in TERMINAL:
                    item.status, item.message = "cancelled", "服务已停止；重新启动后请手动重试"
    for proc in procs:
        _stop_process(proc)


atexit.register(shutdown)


def parse_import(text: str) -> list[dict]:
    """CSV / 制表符 / ----；错误只显示行号，不回显凭据。"""
    if len(text) > 256_000:
        raise ValueError("导入内容过大，请每批不超过 200 个账号")
    text = text.lstrip("\ufeff")
    first = next((line for line in text.splitlines() if line.strip()), "")
    if "----" in first and "," not in first and "\t" not in first:
        rows = [line.split("----") for line in text.splitlines() if line.strip()]
    else:
        try:
            rows = [row for row in csv.reader(io.StringIO(text), delimiter="\t" if "\t" in first else ",", strict=True) if any(row)]
        except csv.Error:
            raise ValueError("CSV 格式不正确，请检查引号和列数") from None
    if rows and rows[0][0].strip().lower() in {"username", "handle"} and len(rows[0]) > 1 and rows[0][1].strip().lower() == "password":
        rows = rows[1:]
    if not 1 <= len(rows) <= 200:
        raise ValueError("每批需要 1–200 个账号")
    result, seen = [], set()
    for line, row in enumerate(rows, 1):
        if not 2 <= len(row) <= 5:
            raise ValueError(f"第 {line} 行应为 2–5 列：用户名、密码、TOTP 密钥、邮箱、代理")
        creds = dict(zip(("username", "password", "totp_secret", "email", "proxy"), row))
        handle = creds["username"].strip().lstrip("@")
        if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", handle) or not creds["password"]:
            raise ValueError(f"第 {line} 行的用户名或密码为空/格式不正确；首列请填 handle，不填邮箱")
        if handle.lower() in seen:
            raise ValueError(f"第 {line} 行的账号在本批内重复，请去重后导入")
        seen.add(handle.lower())
        creds["username"] = handle
        creds["totp_secret"] = normalize_totp_secret(creds.get("totp_secret"))
        if validate_unofficial_credentials(creds):
            raise ValueError(f"第 {line} 行的 TOTP 密钥格式不正确，应填写绑定验证器时的密钥，不是六位验证码")
        if creds["totp_secret"]:
            import pyotp
            try:
                pyotp.TOTP(creds["totp_secret"]).now()
            except Exception:
                raise ValueError(f"第 {line} 行的 TOTP 密钥不能生成验证码，请检查完整性") from None
        result.append({"handle": handle, "credentials": creds})
    return result


def import_accounts(records: list[dict]) -> tuple[list[int], int]:
    """同名账号（包括软删除账号）一律跳过，不覆盖已有凭据/策略。整批事务提交。"""
    ids, skipped = [], 0
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        for record in records:
            if conn.execute("SELECT 1 FROM accounts WHERE lower(handle)=lower(?)", (record["handle"],)).fetchone():
                skipped += 1
                continue
            row = conn.execute("INSERT INTO accounts(handle,access_type,credentials,status,created_at) VALUES (?,'unofficial',?,'auth_error',?)",
                               (record["handle"], json.dumps(record["credentials"], ensure_ascii=False), utcnow_iso()))
            ids.append(row.lastrowid)
        conn.commit()
    return ids, skipped


def pending_accounts() -> list[dict]:
    """重启/关闭下载提示后，仍可从数据库恢复等待登录的账号名单。"""
    with get_conn() as conn:
        rows = conn.execute("SELECT id,handle,credentials,status FROM accounts WHERE deleted_at IS NULL "
                            "AND access_type='unofficial' AND is_primary=0 ORDER BY id").fetchall()
    result = []
    for row in rows:
        creds = parse_credentials(row["credentials"])
        if creds.get("username") and creds.get("password") and (
                row["status"] == "auth_error" or not (creds.get("auth_token") and creds.get("ct0"))):
            result.append({"id": row["id"], "handle": row["handle"]})
    return result
