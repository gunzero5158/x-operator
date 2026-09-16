"""共享展示主题；样式文件独立维护，刷新页面即可读取新的 CSS。"""
from pathlib import Path

from nicegui import ui


def apply_theme() -> None:
    ui.colors(primary="#3264d4", secondary="#64748b", accent="#7255a5",
              positive="#218467", negative="#c04451", warning="#a96715", info="#3264d4")
    ui.add_css(Path(__file__).with_suffix(".css"))
