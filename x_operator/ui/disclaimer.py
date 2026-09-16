"""页面底部的使用声明入口；仅在用户主动打开时展示全文。"""
from nicegui import ui


DISCLAIMER = """本项目仅供**学习、研究与技术测试**使用。

- 使用者应自行遵守 X（Twitter）的服务条款、开发者协议及所在国家和地区的法律法规。因违反相关条款或法律造成的任何后果，由使用者自行承担。
- 自动化操作存在**账号被限流、封禁**的风险。请只在有权操作的账号上使用，并保守设置发送频率。
- 本项目不对使用过程中产生的任何直接或间接损失负责，包括账号损失、数据丢失、API 费用支出。
- 请勿用于骚扰、垃圾信息群发、虚假宣传或任何违法违规用途。
- 项目中提到的第三方服务与本项目无从属关系，使用前请自行评估其可靠性与合规性。

继续使用即表示你已阅读并同意以上条款。
"""


def show_disclaimer() -> None:
    with ui.dialog() as dialog, ui.card().classes("xo-disclaimer-dialog"):
        with ui.row().classes("w-full items-center justify-between no-wrap"):
            ui.label("免责声明").classes("text-lg font-semibold").props('role=heading aria-level=2')
            ui.button(icon="close", on_click=dialog.close).props('flat round dense aria-label="关闭免责声明"')
        ui.markdown(DISCLAIMER).classes("xo-disclaimer-copy")
        with ui.row().classes("w-full justify-end"):
            ui.button("关闭", on_click=dialog.close).props("flat")
    dialog.on("hide", dialog.delete)
    dialog.open()


def disclaimer_footer() -> None:
    with ui.element("footer").classes("xo-footer"):
        ui.label("仅供学习、研究与技术测试")
        ui.button("免责声明", on_click=show_disclaimer).props("flat dense no-caps color=secondary").classes("xo-footer-link")
