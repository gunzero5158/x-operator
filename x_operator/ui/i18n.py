"""UI-only translations. Stored content, identifiers and model prompts stay unchanged."""
from __future__ import annotations

import json
import re
from copy import copy
from functools import lru_cache
from pathlib import Path

from nicegui import app, ui
from nicegui.slot import Slot

LANGUAGES = {"zh-CN": "简体中文", "zh-Hant": "繁體中文", "en": "English", "ja": "日本語"}
DEFAULT_LANGUAGE = "zh-CN"


def detect_language(accept_language: str) -> str:
    """Match the browser's ordered language preferences to the shipped UI catalogs."""
    preferences = []
    for item in accept_language.split(","):
        parts = item.strip().split(";")
        tag = parts[0].strip().lower().replace("_", "-")
        quality = 1.0
        for param in parts[1:]:
            key, _, value = param.strip().partition("=")
            if key.lower() == "q":
                try:
                    quality = float(value)
                except ValueError:
                    quality = 0.0
        if 0 < quality <= 1:
            preferences.append((quality, tag))
    # Python's stable sort preserves the browser's order for equal priorities.
    for _, tag in sorted(preferences, key=lambda item: -item[0]):
        subtags = tag.split("-")
        if subtags[0] == "zh":
            # An explicit script takes precedence over the region (e.g. zh-Hans-HK).
            # Ignore extensions/private-use subtags when looking for a region.
            base = []
            for subtag in subtags[1:]:
                if len(subtag) == 1:
                    break
                base.append(subtag)
            if "hant" in base:
                return "zh-Hant"
            if "hans" in base:
                return "zh-CN"
            return "zh-Hant" if any(region in base for region in ("tw", "hk", "mo")) else "zh-CN"
        language = {"en": "en", "ja": "ja"}.get(subtags[0])
        if language:
            return language
    return DEFAULT_LANGUAGE


def current_language() -> str:
    try:
        # Freeze the language for each page so another tab cannot change labels mid-form.
        # Accessing ui.context.client at import time starts NiceGUI's script mode.
        # Read the existing stack only: translation must never create UI implicitly.
        stack = Slot.get_stack()
        return stack[-1].parent.client.storage.get("ui_language", DEFAULT_LANGUAGE) if stack else DEFAULT_LANGUAGE
    except RuntimeError:
        return DEFAULT_LANGUAGE


@lru_cache(maxsize=4)
def catalog(language: str) -> dict[str, str]:
    if language not in LANGUAGES or language == DEFAULT_LANGUAGE:
        return {}
    return json.loads((Path(__file__).parent / "locales" / f"{language}.json").read_text(encoding="utf-8"))


def t(source: str, *, _language: str | None = None, **values) -> str:
    """Translate an explicit UI message, then interpolate untouched user data."""
    translated = catalog(_language or current_language()).get(source, source)
    return translated.format(**values) if values else translated


def localized_props(source: str) -> str:
    """Translate explicit, static accessibility attributes without changing other props."""
    def replace(match):
        # These labels are developer-authored, never form values or user content.
        label = t(match[2]).replace('"', '&quot;')
        return f'{match[1]}="{label}"'
    return re.sub(r'(aria-label|alt)="([^"]+)"', replace, source)


def describe_schedule(job: str) -> str:
    """Present the core schedule description without changing scheduling decisions."""
    from ..core.scheduler import describe_schedule as core_description
    source = core_description(job)
    interval = re.fullmatch(r"每隔 (\d+) 分钟（从程序启动/改设置那一刻起算）", source)
    if interval:
        return t("每隔 {minutes} 分钟（从程序启动/改设置那一刻起算）", minutes=interval[1])
    daily = re.fullmatch(r"每天 (.+)（([^（）]+)）", source)
    if daily:
        return t("每天 {times}（{timezone}）", times=daily[1].replace("、", ", "), timezone=daily[2])
    return t(source)


def _display_value(value):
    if isinstance(value, str):
        return t(value)
    if isinstance(value, tuple):
        return tuple(_display_value(item) for item in value)
    return value


class Labels(dict):
    """A UI-owned copy of static labels, resolved for the current client on lookup.

    Core label dictionaries are also used in logs and prompts. Copying them here
    avoids changing backend behavior or binding module globals to the first locale.
    """

    def __getitem__(self, key):
        return _display_value(super().__getitem__(key))

    def get(self, key, default=None):
        return _display_value(super().get(key, default))

    def items(self):
        return ((key, _display_value(value)) for key, value in super().items())

    def values(self):
        return (_display_value(value) for value in super().values())


def initialize_language() -> None:
    try:
        mode = app.storage.user.get("ui_language", "auto")
    except RuntimeError:
        mode = "auto"
    if mode not in LANGUAGES:
        mode = "auto"
    try:
        browser_languages = ui.context.client.request.headers.get("accept-language", "")
    except RuntimeError:
        browser_languages = ""
    language = detect_language(browser_languages) if mode == "auto" else mode
    ui.context.client.storage["ui_language_mode"] = mode
    ui.context.client.storage["ui_language"] = language
    ui.add_head_html(f'<meta name="x-operator-language" content="{language}">')
    # Page definitions are shared; copy before setting the per-client Quasar/HTML locale.
    page = copy(ui.context.client.page)
    page.language = {"zh-CN": "zh-CN", "zh-Hant": "zh-TW", "en": "en-US", "ja": "ja"}[language]
    ui.context.client.page = page


def language_menu() -> None:
    mode = ui.context.client.storage.get("ui_language_mode", "auto")

    async def choose(language: str) -> None:
        if language == mode:
            return
        # Explicitly warn about unsaved forms before navigating away.
        with ui.dialog() as dialog, ui.card().classes("max-w-md"):
            ui.label(t("切换界面语言")).classes("font-semibold")
            ui.label(t("切换后会刷新当前页面，请先保存尚未提交的表单。后台任务会继续运行。"))
            with ui.row().classes("w-full justify-end"):
                ui.button(t("取消"), on_click=lambda: dialog.submit(False)).props("flat")
                ui.button(t("切换"), on_click=lambda: dialog.submit(True))
        if await dialog:
            app.storage.user["ui_language"] = language
            ui.navigate.reload()
        dialog.delete()

    label = LANGUAGES[current_language()]
    if mode == "auto":
        label = t("自动 · {language}", language=label)
    with ui.button(label, icon="language").props(
        'flat dense no-caps aria-label="Interface language / 界面语言 / 表示言語"'
    ).classes("xo-language"):
        with ui.menu():
            for code, label in {"auto": t("跟随系统（浏览器语言）"), **LANGUAGES}.items():
                ui.menu_item(label, on_click=lambda code=code: choose(code)) \
                    .props("active" if code == mode else "").mark(f"language-{code}")
