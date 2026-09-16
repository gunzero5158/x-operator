"""共享展示主题；样式文件独立维护，刷新页面即可读取新的 CSS。"""
from pathlib import Path

from nicegui import ui


def apply_theme() -> None:
    # 主色以 theme.css 的 --xo-accent 为准（那边用 !important 覆盖 Quasar 变量），这里填同一个值免得两处打架
    ui.colors(primary="#61508E", secondary="#686175", accent="#61508E",
              positive="#218467", negative="#c04451", warning="#a96715", info="#61508E")
    ui.add_css(Path(__file__).with_suffix(".css"))
