"""界面弹窗冒烟：用 NiceGUI 的 User 模拟器打开各页面、点开弹窗，确认附件区 / 方式一二分区 / 标签图例都能渲染。

运行（不改项目依赖，临时装 pytest）：
    uv run --with pytest --with pytest-asyncio pytest scripts/ui_dialog_check.py -q -o asyncio_mode=auto
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

os.environ["X_OPERATOR_MOCK"] = "1"

from nicegui import ui  # noqa: E402
from nicegui.testing import User  # noqa: E402
from nicegui.testing.user_interaction import UserInteraction  # noqa: E402

from x_operator.core import media  # noqa: E402
from x_operator.core.scheduler import Jobs  # noqa: E402
from x_operator.db.database import get_conn, init_db, utcnow_iso  # noqa: E402
from x_operator.ui import materials, queue, schedule, settings_page, targets  # noqa: E402

pytest_plugins = ["nicegui.testing.user_plugin"]

TMP = Path(tempfile.mkdtemp(prefix="xop_ui_"))
init_db(TMP / "t.db")
with get_conn() as conn:
    conn.execute("INSERT INTO accounts(handle, display_name, access_type, is_primary, credentials) VALUES ('acc1','','official',1,'{}')")
    conn.execute("INSERT INTO accounts(handle, display_name, access_type, is_primary, credentials) "
                 "VALUES ('small1','','unofficial',0,'{\"username\": \"u\", \"password\": \"p\"}')")
    rel = media.new_rel_path("a.png")
    media.abs_path(rel).parent.mkdir(parents=True, exist_ok=True); media.abs_path(rel).write_bytes(b"x")
    conn.execute("INSERT INTO materials(kind,text,lang,status,media_files) VALUES ('reply','带图素材','ja','active',?)", (media.dump_files([rel]),))
    conn.execute("INSERT INTO materials(kind,text,lang,status) VALUES ('post','发帖素材','ja','draft')")
    conn.execute("INSERT INTO target_tweets(tweet_id, author_id, author_handle, text, lang, tweet_created_at, source, process_status) "
                 "VALUES ('1','9','someone','hello','en',?, 'search','queued')", (utcnow_iso(),))
    conn.execute("INSERT INTO review_queue(account_id, action_type, target_tweet_id, material_id, final_text, final_media_files, status, created_at) "
                 "VALUES (1,'reply',1,1,'reply text',?, 'pending',?)", (media.dump_files([rel]), utcnow_iso()))
    conn.commit()
JOBS = Jobs()


def _choose(user: User, label, key, kind=ui.select) -> None:
    """像用户那样在下拉 / 切换按钮里选一项：模拟器直接 set_value 不会触发页面联动，要按 NiceGUI 的内部事件格式触发。"""
    inter = user.find(label) if label else user.find(kind=kind)
    el = [e for e in inter.elements if isinstance(e, kind)][0]
    idx = list(el.options).index(key)
    UserInteraction(user, {el}, None).trigger("update:modelValue", {"value": idx} if kind is ui.select else idx)


def _pages() -> None:
    """user fixture 每个测试前会清空页面注册，所以在测试里注册。"""
    for mod in (materials, queue, schedule, settings_page, targets):
        mod.register(JOBS)


async def test_materials_legend_and_dialog(user: User):
    _pages()
    await user.open("/materials")
    await user.should_see("标签说明")
    await user.should_see("📎 1 张图片")
    user.find("新建素材").click()
    await user.should_see("配图 / 视频（选填）")
    await user.should_see("没有附件（纯文字）")
    await user.should_see(kind=ui.upload)


async def test_material_edit_shows_existing_attachment(user: User):
    _pages()
    await user.open("/materials")
    user.find("编辑").click()
    await user.should_see("1 张图片")
    await user.should_see(kind=ui.image)


async def test_queue_attachment_button_and_dialog(user: User):
    _pages()
    await user.open("/queue")
    await user.should_see("📎 1 张图片")
    user.find("附件（1）").click()
    await user.should_see("这条的配图 / 视频")
    await user.should_see("保存附件")


async def test_schedule_dialog_has_media_field(user: User):
    _pages()
    await user.open("/schedule")
    user.find("新建计划").click()
    await user.should_see("内容来源")
    _choose(user, "每次发什么", "ai_topic")
    await user.should_see("每次随帖一起发的配图 / 视频（选填）")


async def test_account_dialog_methods_are_distinct(user: User):
    _pages()
    await user.open("/settings")
    user.find("添加账号").click()
    await user.should_see("通道类型")
    _choose(user, "通道类型", "unofficial")
    await user.should_see("下面两种登录方式选一种填就行")
    await user.should_see("方式一：浏览器 Cookie")
    await user.should_not_see("方式二：账号密码 + 两步验证密钥")
    _choose(user, None, "password", kind=ui.toggle)
    await user.should_see("方式二：账号密码 + 两步验证密钥")
    await user.should_not_see("方式一：浏览器 Cookie")


async def test_existing_password_account_opens_on_method_two(user: User):
    _pages()
    await user.open("/settings")
    # small1 只存了账号密码：编辑弹窗应默认停在方式二（找 small1 卡片里的编辑按钮）
    btn = [b for b in user.find("编辑 / 填凭据").elements if "small1" in _card_text(b)]
    assert len(btn) == 1, [_card_text(b)[:60] for b in user.find("编辑 / 填凭据").elements]
    UserInteraction(user, set(btn), None).click()
    await user.should_see("方式二：账号密码 + 两步验证密钥")
    await user.should_not_see("方式一：浏览器 Cookie")


def _card_text(el) -> str:
    """向上找到所在卡片，把里面所有 label/badge 文本拼起来。"""
    node = el
    while node is not None and type(node).__name__ != "Card":
        node = node.parent_slot.parent if node.parent_slot else None
    if node is None:
        return ""
    out = []
    def walk(e):
        t = getattr(e, "text", None)
        if isinstance(t, str):
            out.append(t)
        for slot in e.slots.values():
            for c in slot.children:
                walk(c)
    walk(node)
    return " ".join(out)
