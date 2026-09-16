"""页面底部的使用声明入口；仅在用户主动打开时展示全文。"""
from nicegui import ui


DISCLAIMER = """本项目按 **Apache License 2.0** 以“现状”提供，不作任何明示或默示保证。以下内容是使用风险提示，不对该许可证授予的权利增加限制。

许可证全文见项目根目录的 LICENSE 文件。该许可证不代表 X 或其他第三方授权，也不保证任何具体使用方式符合平台规则或法律要求。

- 使用者应自行评估并遵守适用的 X（Twitter）服务条款、开发者协议及所在国家和地区的法律法规，承担依法应由自身承担的责任。
- 自动化操作存在**账号被限流、封禁**的风险。请只在有权操作的账号上使用，并保守设置发送频率。
- 在适用法律允许的范围内，作者和贡献者不对使用过程中产生的损失承担责任，包括账号损失、数据丢失、API 费用支出；具体以许可证第 7、8 条为准，不排除依法不得排除的责任。
- 请勿用于骚扰、垃圾信息群发、虚假宣传或任何违法违规用途。
- 项目中提到的第三方服务与本项目无从属关系，使用前请自行评估其可靠性与合规性。

使用前请阅读许可证，并自行评估上述风险。
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
        ui.label("Apache-2.0 开源许可 · 请合理使用")
        ui.button("免责声明", on_click=show_disclaimer).props("flat dense no-caps color=secondary").classes("xo-footer-link")
