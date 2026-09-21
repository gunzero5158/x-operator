"""Focused UI smoke tests with a disposable database; never contacts X or an LLM."""
from __future__ import annotations

import ast
import json
import os
import string
import subprocess
import sys
import tomllib
from pathlib import Path

os.environ["X_OPERATOR_MOCK"] = "1"

import pytest
from nicegui import app, ui
from nicegui.testing import User
from nicegui.testing.user_interaction import UserInteraction

from scripts.release_version import bump, change_level
from x_operator.core.scheduler import Jobs
from x_operator.db.database import get_conn, init_db
from x_operator.db import database
from x_operator.ui import dashboard, materials, queue, rules, schedule, settings_page, targets, watched
from x_operator.ui.i18n import Labels, catalog, current_language, detect_language, t
from x_operator.version import __version__

pytest_plugins = ["nicegui.testing.user_plugin"]
ROOT = Path(__file__).resolve().parents[1]
PAGES = (dashboard, materials, queue, rules, schedule, settings_page, targets, watched)
ROUTES = ("/", "/materials", "/queue", "/rules", "/schedule", "/settings", "/targets", "/watched")


@pytest.fixture
def pages(tmp_path):
    if getattr(database._local, "conn", None) is not None:
        database._local.conn.close()
        del database._local.conn
    init_db(tmp_path / "ui.db")
    with get_conn() as conn:
        conn.execute("INSERT INTO materials(kind,text,lang,status) VALUES ('reply','保存','ja','active')")
        conn.commit()
    jobs = Jobs()  # The scheduler is deliberately not started.
    for module in PAGES:
        module.register(jobs)


@pytest.mark.parametrize("language", ["zh-CN", "zh-Hant", "en", "ja"])
async def test_all_pages_render(user: User, pages, language):
    await user.open("/")
    with user:
        app.storage.user["ui_language"] = language
    for route in ROUTES:
        await user.open(route)
        await user.should_see(f"v{__version__}")
        await user.should_see(t("免责声明", _language=language))
        with user:
            assert current_language() == language
            assert ui.context.client.page.language == {"en": "en-US", "ja": "ja", "zh-CN": "zh-CN", "zh-Hant": "zh-TW"}[language]


@pytest.mark.parametrize("language", ["en", "ja", "zh-Hant"])
async def test_material_form_keeps_user_content_and_option_keys(user: User, pages, language):
    await user.open("/")
    with user:
        app.storage.user["ui_language"] = language
    await user.open("/materials")
    await user.should_see("保存")  # User text matching a UI translation key must remain untouched.
    user.find(t("新建素材", _language=language)).click()
    await user.should_see(t("正文", _language=language))
    selectors = [el for el in user.find(kind=ui.select).elements if el.label == t("语言", _language=language)]
    selector = selectors[-1]
    assert selector.value == "auto" and "ja" in selector.options and "zh-Hant" in selector.options
    text_input = next(iter(user.find(kind=ui.textarea).elements))
    text_input.set_value("今日は新しいツールを試してみました")
    buttons = [el for el in user.find(kind=ui.button).elements if el.text == t("保存", _language=language)]
    UserInteraction(user, {buttons[-1]}, None).click()
    await user.should_see(t("已保存", _language=language))
    with get_conn() as conn:
        row = conn.execute("SELECT lang FROM materials WHERE text=?", (text_input.value,)).fetchone()
    assert row["lang"] == "ja"


async def test_language_switch_confirm_and_persistence(user: User, pages):
    await user.open("/materials?kind=reply")
    user.find(kind=ui.button, content="简体中文").click()
    user.find("language-en").click()
    await user.should_see("切换界面语言")
    user.find(kind=ui.button, content="取消").click()
    await user.should_not_see("切换界面语言")
    with user:
        assert current_language() == "zh-CN"
    user.find("language-en").click()
    await user.should_see("切换界面语言")
    user.find(kind=ui.button, content="切换").click()
    await user.should_see("Media library")
    assert user.back_history[-1] == "/materials?kind=reply"
    await user.open("/settings")
    with user:
        assert current_language() == "en"
    await user.should_see("Settings")


def test_catalogs_cover_explicit_messages_and_preserve_placeholders():
    def fields(text):
        return {name for _, name, _, _ in string.Formatter().parse(text) if name is not None}
    for language in ("en", "ja", "zh-Hant"):
        messages = catalog(language)
        assert len(messages) > 1000
        for source, translation in messages.items():
            assert translation.strip(), source
            assert fields(source) == fields(translation), source
        for path in (ROOT / "x_operator/ui").glob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {"_tr", "t"}:
                    if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                        source = node.args[0].value
                        assert source in messages, (path.name, node.lineno, source)


def test_translation_does_not_interpret_user_placeholders():
    value = '{p1} <script> $HOME 日本語'
    assert t("已添加 {p0}", _language="en", p0=value) == "Added " + value
    assert t("unrecognized provider error", _language="ja") == "unrecognized provider error"
    assert t("已添加 {p0}", _language="zh-Hant", p0="保存 {p1}") == "已新增 保存 {p1}"


async def test_traditional_manual_selection_overrides_browser(user: User, pages):
    user.http_client.headers["Accept-Language"] = "en-US"
    await user.open("/materials?kind=reply")
    user.find(kind=ui.button, content="Auto · English").click()
    user.find("language-zh-Hant").click()
    await user.should_see(t("切换界面语言", _language="en"))
    user.find(kind=ui.button, content=t("切换", _language="en")).click()
    await user.should_see("素材庫")
    await user.should_see("保存")
    assert user.back_history[-1] == "/materials?kind=reply"
    user.http_client.headers["Accept-Language"] = "ja-JP"
    await user.open("/settings")
    with user:
        assert current_language() == "zh-Hant"
        assert app.storage.user["ui_language"] == "zh-Hant"
        assert ui.context.client.page.language == "zh-TW"
    await user.should_see("設定")


@pytest.mark.parametrize("messages,level", [
    ("fix: queue limit\nstyle: spacing", "patch"),
    ("fix: labels\nfeat(ui): language menu", "minor"),
    ("feat!: incompatible settings", "major"),
    ("fix: config\n\nBREAKING CHANGE: old config removed", "major"),
    ("Update translations", "patch"),
])
def test_version_scope(messages, level):
    assert change_level(messages) == level
    assert bump("1.2.3", level) == {"patch": "1.2.4", "minor": "1.3.0", "major": "2.0.0"}[level]


def test_single_version_source():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert "version" in project["project"]["dynamic"]
    assert project["tool"]["hatch"]["version"]["path"] == "x_operator/version.py"


@pytest.mark.parametrize("header,expected", [
    ("en-US,en;q=0.9", "en"), ("ja-JP,en;q=0.5", "ja"),
    ("zh-TW,zh-Hant;q=0.9,en;q=0.8", "zh-Hant"),
    ("zh-HK", "zh-Hant"), ("zh-MO", "zh-Hant"), ("zh-Hant", "zh-Hant"),
    ("zh-Hant-CN", "zh-Hant"), ("zh-Hans-HK", "zh-CN"),
    ("zh-SG", "zh-CN"), ("zh-CN", "zh-CN"), ("zh", "zh-CN"),
    ("zh_Hant_TW", "zh-Hant"), ("zh-CN-x-hk", "zh-CN"),
    ("zh-TW;q=0.5,en;q=0.9", "en"), ("zh-TW;q=0,zh-CN", "zh-CN"),
    ("en;q=0.2,ja;q=0.9", "ja"), ("fr-FR,ja;q=0.8", "ja"),
    ("ja;q=0,en;q=0.5", "en"), ("ja;q=invalid,en", "en"),
    ("ja;q=2,en;q=0.8", "en"), ("ja;q=NaN,en", "en"),
    ("JA-jp,en", "ja"), ("de-DE", "zh-CN"), ("", "zh-CN"),
])
def test_browser_language_preferences(header, expected):
    assert detect_language(header) == expected


async def test_auto_language_manual_override_and_return_to_auto(user: User, pages):
    user.http_client.headers["Accept-Language"] = "ja-JP,en;q=0.8"
    await user.open("/materials?kind=reply")
    await user.should_see("自動 · 日本語")
    with user:
        assert current_language() == "ja"
        assert "ui_language" not in app.storage.user  # Auto detection never becomes a manual preference.
    user.find(kind=ui.button, content="自動 · 日本語").click()
    user.find("language-ja").click()  # Pin the same language; must not be treated as a no-op.
    await user.should_see(t("切换界面语言", _language="ja"))
    user.find(kind=ui.button, content=t("切换", _language="ja")).click()
    await user.should_not_see("自動 · 日本語")
    await user.should_see("素材ライブラリー")
    with user:
        assert app.storage.user["ui_language"] == "ja"
    user.http_client.headers["Accept-Language"] = "en-US"
    await user.open("/settings")
    with user:
        assert current_language() == "ja"
    user.find(kind=ui.button, content="日本語").click()
    user.find("language-auto").click()
    await user.should_see(t("切换界面语言", _language="ja"))
    user.find(kind=ui.button, content=t("切换", _language="ja")).click()
    await user.should_see("Auto · English")
    with user:
        assert current_language() == "en" and app.storage.user["ui_language"] == "auto"
    user.http_client.headers["Accept-Language"] = "zh-HK"
    await user.open("/")
    await user.should_see("自動 · 繁體中文")


def test_importing_ui_does_not_start_script_mode():
    result = subprocess.run([sys.executable, "-c",
        "from x_operator.ui import dashboard, settings_page; from nicegui import core; assert not core.script_mode"],
        cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("language", ["en", "ja", "zh-Hant"])
async def test_credentials_and_table_keep_data_fields(user: User, pages, language):
    with get_conn() as conn:
        conn.execute("INSERT INTO action_log(api_kind,endpoint,error,success) VALUES ('x_mock','SMOKE-ONLY','original error',0)")
        conn.commit()
    await user.open("/")
    with user:
        app.storage.user["ui_language"] = language
    await user.open("/")
    user.find(kind=ui.expansion, content=t("最近异常 · 近 24 小时 {count} 次", _language=language, count=1)).click()
    tables = user.find(kind=ui.table).elements
    error_table = next(table for table in tables if any(row.get("来源") == "SMOKE-ONLY" for row in table.rows))
    for column in error_table.columns:
        assert column["field"] in error_table.rows[0]
    await user.open("/settings")
    user.find(kind=ui.button, content=t("添加账号", _language=language)).click()
    for key, source, secret in settings_page._COOKIE_FIELDS:
        await user.should_see(t(source, _language=language))
    # Static credential instructions also resolve when their collapsed guide is opened.
    guide_title = t("怎么拿到 auth_token / ct0？密码 + 两步验证怎么填？（点开看手把手步骤）", _language=language)
    guide = next(el for el in user.find(kind=ui.expansion).elements if el.text == guide_title)
    UserInteraction(user, {guide}, None).click()
    await user.should_see(t(settings_page._COOKIE_GUIDE, _language=language)[:20])


def test_progress_calls_have_language_independent_task_keys():
    for path in (ROOT / "x_operator/ui").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "run_job_with_progress":
                assert any(kw.arg == "task_key" for kw in node.keywords), (path.name, node.lineno)


def test_version_command_changes_only_once_per_release(tmp_path, monkeypatch):
    from scripts import release_version
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=tmp_path, text=True).strip()
    git("init", "-q")
    git("config", "user.name", "Local test")
    git("config", "user.email", "test@example.invalid")
    version = tmp_path / "x_operator/version.py"
    version.parent.mkdir()
    version.write_text('__version__ = "1.2.3"\n')
    git("add", ".")
    git("commit", "-qm", "chore: baseline")
    base = git("rev-parse", "HEAD")
    (tmp_path / "feature.txt").write_text("new feature")
    git("add", ".")
    git("commit", "-qm", "feat: new interface")
    monkeypatch.setattr(release_version, "ROOT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["release_version.py", "--base", base, "--dry-run"])
    release_version.main()
    assert '"1.2.3"' in version.read_text()
    monkeypatch.setattr(sys, "argv", ["release_version.py", "--base", base])
    release_version.main()
    assert '"1.3.0"' in version.read_text()
    release_version.main()
    assert '"1.3.0"' in version.read_text()
