"""程序入口：初始化 DB → 启动补扫 → 注册 UI → 起调度器 → ui.run。

运行：uv run python -m x_operator.main
"""
from __future__ import annotations

import logging
from pathlib import Path

import tomllib
from nicegui import app, ui

from .core.scheduler import Jobs, build_scheduler, run_startup_recovery
from .db.database import init_db
from .ui import (dashboard, materials, queue, rules, schedule, settings_page,
                 targets, watched)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("x_operator")

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_toml() -> dict:
    path = PROJECT_ROOT / "config" / "settings.toml"
    if path.exists():
        with open(path, "rb") as f:
            return tomllib.load(f)
    return {}


def main() -> None:
    conf = load_toml()
    app_conf = conf.get("app", {})
    # 数据目录相对于项目根目录（而不是启动时所在的目录），从别处启动也能找到同一个库
    data_dir = Path(conf.get("data", {}).get("dir", "data"))
    if not data_dir.is_absolute():
        data_dir = PROJECT_ROOT / data_dir
    port = int(app_conf.get("port", 8080))
    # 默认只监听本机：页面没有登录保护，账号密码/Cookie 都在设置页明文可见，绝不能暴露到局域网
    host = str(app_conf.get("host", "127.0.0.1"))

    db_path = data_dir / "x_operator.db"
    init_db(db_path, setting_overrides=conf.get("defaults") or {})
    log.info("数据库就绪：%s", db_path)

    jobs = Jobs()
    msgs = run_startup_recovery(jobs)
    for m in msgs:
        log.info("启动补扫：%s", m)

    # 注册所有页面
    for mod in (dashboard, queue, targets, materials, watched, rules, schedule, settings_page):
        mod.register(jobs)

    scheduler = build_scheduler(jobs)
    scheduler.start()
    app.on_shutdown(lambda: scheduler.shutdown(wait=False))

    if host not in ("127.0.0.1", "localhost", "::1"):
        log.warning("正在监听 %s：页面没有登录保护，同一网络里的任何人都能看到账号凭据并操作发送！", host)
    ui.run(host=host, port=port, title="x-operator", show=False, reload=False,
           storage_secret="x-operator-mvp")


# NiceGUI 要求在模块级调用 ui.run（通过 __main__ 保护）
if __name__ in {"__main__", "__mp_main__"}:
    main()
