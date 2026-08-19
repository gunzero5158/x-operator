"""程序入口：初始化 DB → 启动补扫 → 注册 UI → 起调度器 → ui.run。

运行：uv run python -m x_operator.main
"""
from __future__ import annotations

import logging
from pathlib import Path

import tomllib
from nicegui import app, ui

from . import config
from .core.scheduler import Jobs, build_scheduler, run_startup_recovery
from .db.database import init_db
from .ui import (dashboard, materials, queue, rules, schedule, settings_page,
                 watched)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("x_operator")


def load_toml() -> dict:
    path = Path(__file__).resolve().parent.parent / "config" / "settings.toml"
    if path.exists():
        with open(path, "rb") as f:
            return tomllib.load(f)
    return {}


def main() -> None:
    conf = load_toml()
    data_dir = Path(conf.get("data", {}).get("dir", "data"))
    port = int(conf.get("app", {}).get("port", 8080))

    db_path = data_dir / "x_operator.db"
    init_db(db_path)
    log.info("数据库就绪：%s", db_path)

    jobs = Jobs()
    msgs = run_startup_recovery(jobs)
    for m in msgs:
        log.info("启动补扫：%s", m)

    # 注册所有页面
    for mod in (dashboard, queue, materials, watched, rules, schedule, settings_page):
        mod.register(jobs)

    scheduler = build_scheduler(jobs)
    scheduler.start()
    app.on_shutdown(lambda: scheduler.shutdown(wait=False))

    ui.run(port=port, title="x-operator", show=False, reload=False,
           storage_secret="x-operator-mvp")


# NiceGUI 要求在模块级调用 ui.run（通过 __main__ 保护）
if __name__ in {"__main__", "__mp_main__"}:
    main()
