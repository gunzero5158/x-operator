"""界面弹窗冒烟：用 NiceGUI 的 User 模拟器打开各页面、点开弹窗，确认附件区 / 方式一二分区 / 标签图例都能渲染。

运行（不改项目依赖，临时装 pytest）：
    uv run --with pytest --with pytest-asyncio pytest scripts/ui_dialog_check.py -q -o asyncio_mode=auto -o main_file=
（新版 NiceGUI 的 user 模拟器默认找根目录 main.py，本项目没有，用 -o main_file= 关掉）
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
from x_operator.ui import dashboard, materials, queue, rules, schedule, settings_page, targets  # noqa: E402

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


def _click_button(user: User, label: str) -> None:
    """只点按钮：user.find(文字) 是子串匹配，页面上别的小字含同样的词时会点错。"""
    btns = [e for e in user.find(kind=ui.button).elements if e.text == label]
    assert btns, f"没有文字为「{label}」的按钮"
    UserInteraction(user, {btns[0]}, None).click()


def _pages() -> None:
    """user fixture 每个测试前会清空页面注册，所以在测试里注册。"""
    for mod in (dashboard, materials, queue, rules, schedule, settings_page, targets):
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


async def test_material_new_defaults_to_auto_lang_and_saves_detected(user: User):
    """新建素材：语言默认「自动判断」，输入正文后小字实时显示识别结果，保存落库为识别出的语言码。"""
    _pages()
    await user.open("/materials")
    user.find("新建素材").click()
    await user.should_see("自动判断：输入正文后会按内容识别语言")
    sel = [e for e in user.find("语言").elements if isinstance(e, ui.select)][0]
    assert sel.value == "auto" and "auto" in sel.options and "ja" in sel.options and "ko" in sel.options, (sel.value, sel.options)
    ta = [e for e in user.find(kind=ui.textarea).elements][0]
    ta.set_value("今日は新しいツールを試してみました")
    await user.should_see("识别为「日语」")
    ta.set_value("This tool is great for your team")
    await user.should_see("识别为「英语」")
    _click_button(user, "保存")
    await user.should_see("已保存")
    with get_conn() as conn:
        row = conn.execute("SELECT lang FROM materials WHERE text=?", ("This tool is great for your team",)).fetchone()
    assert row and row["lang"] == "en", dict(row) if row else row


async def test_material_manual_lang_overrides_auto(user: User):
    """手选托底：语言下拉手选「中文」后，即使正文是英文也按手选存；判不出语言且没手选时会拦下来。"""
    _pages()
    await user.open("/materials")
    user.find("新建素材").click()
    ta = [e for e in user.find(kind=ui.textarea).elements][0]
    ta.set_value("🎉🎉 https://example.com")
    await user.should_see("暂时判不出语言")
    _click_button(user, "保存")
    await user.should_see("请在「语言」里手选一个")
    _choose(user, "语言", "zh")
    await user.should_see("手选：中文")
    _click_button(user, "保存")
    await user.should_see("已保存")
    with get_conn() as conn:
        row = conn.execute("SELECT lang FROM materials WHERE text=?", ("🎉🎉 https://example.com",)).fetchone()
    assert row and row["lang"] == "zh", dict(row) if row else row


async def test_material_edit_keeps_stored_lang(user: User):
    """编辑已有素材：语言下拉显示原来存的语言（不是自动），小字提示手选。"""
    _pages()
    await user.open("/materials")
    user.find("编辑").click()
    sel = [e for e in user.find("语言").elements if isinstance(e, ui.select)][0]
    assert sel.value == "ja", sel.value
    await user.should_see("手选：日语")


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
    user.find("新建发帖计划").click()
    await user.should_see("内容来源")
    _choose(user, "每次发什么", "ai_topic")
    await user.should_see("每次随帖一起发的配图 / 视频（选填）")
    sel = [e for e in user.find("节奏").elements if isinstance(e, ui.select)][0]
    assert "interval" in sel.options and "cron" not in sel.options, sel.options   # 下拉里有「每隔」、没有 cron
    _choose(user, "节奏", "interval")
    assert [e for e in user.find(kind=ui.input).elements if e.value == "6h"], "选「每隔」后表达式应换成默认 6h"


async def test_schedule_media_pool_mode(user: User):
    """定时发帖（AI 主题）：附件默认「固定」、上限 4；切到「素材池」后上限变 30、标题和说明跟着换。"""
    _pages()
    await user.open("/schedule")
    user.find("新建发帖计划").click()
    _choose(user, "每次发什么", "ai_topic")
    await user.should_see("每次随帖一起发的配图 / 视频（选填）")
    await user.should_see("合计最多 4 个")
    _choose(user, "配图 / 视频怎么带", "pool")
    await user.should_see("配图 / 视频素材池（选填，每次随机挑 1 个）")
    await user.should_see("素材池最多放 30 个")
    _choose(user, "配图 / 视频怎么带", "fixed")
    await user.should_see("合计最多 4 个")


async def test_display_timezone_setting(user: User):
    """设置 → 自动运行 里改「界面显示时区」：头部显示当前时区，fmt_time 按新时区转；账号自己的时区不受影响。"""
    from x_operator import config
    from x_operator.ui.layout import fmt_time, refresh_display_tz
    _pages()
    with get_conn() as conn:
        acc_tz_before = conn.execute("SELECT timezone FROM accounts WHERE id=1").fetchone()["timezone"]
    config.set_value("display_timezone", "Asia/Tokyo"); refresh_display_tz()
    assert fmt_time("2026-09-08T12:00:00Z") == "09-08 21:00"
    await user.open("/settings")
    await user.should_see("🕒 Asia/Tokyo")
    user.find("自动运行").click()
    await user.should_see("界面显示时区")
    _choose(user, "界面显示时区", "Asia/Taipei")
    await user.should_see("改为按 Asia/Taipei 显示")
    assert config.get("display_timezone") == "Asia/Taipei"
    assert fmt_time("2026-09-08T12:00:00Z") == "09-08 20:00"
    with get_conn() as conn:
        assert conn.execute("SELECT timezone FROM accounts WHERE id=1").fetchone()["timezone"] == acc_tz_before  # 账号自己的时区不被改动
    await user.open("/queue")
    await user.should_see("🕒 Asia/Taipei")
    config.set_value("display_timezone", "Asia/Tokyo"); refresh_display_tz()


async def test_queue_skipped_recheck(user: User):
    """任务队列「已跳过」：原因显示中文、有「重新判断」按钮；顶部「重新判断全部已跳过」只在该筛选下出现；点了能放回待审核。"""
    _pages()
    with get_conn() as conn:
        conn.execute("INSERT INTO target_tweets(tweet_id, author_id, author_handle, text, lang, tweet_created_at, source, process_status) "
                     "VALUES ('sk1','77','skipper','hey','en',?, 'search','queued')", (utcnow_iso(),))
        tt = conn.execute("SELECT id FROM target_tweets WHERE tweet_id='sk1'").fetchone()["id"]
        conn.execute("INSERT INTO review_queue(account_id, action_type, target_tweet_id, final_text, status, skip_reason, created_at) "
                     "VALUES (1,'reply',?,'skipped reply','skipped','author_in_cooldown',?)", (tt, utcnow_iso()))
        rq = conn.execute("SELECT id FROM review_queue WHERE final_text='skipped reply'").fetchone()["id"]
        conn.commit()
    await user.open("/queue")
    status_sel = [e for e in user.find(kind=ui.select).elements if "skipped" in e.options][0]   # 顶部状态筛选（没有 label）
    UserInteraction(user, {status_sel}, None).trigger("update:modelValue", {"value": list(status_sel.options).index("skipped")})
    await user.should_see("作者冷却期内")
    await user.should_see("重新判断全部已跳过")
    _click_button(user, "重新判断")
    await user.should_see("已放回待审核")
    with get_conn() as conn:
        assert conn.execute("SELECT status FROM review_queue WHERE id=?", (rq,)).fetchone()["status"] == "pending"
        conn.execute("DELETE FROM review_queue WHERE id=?", (rq,)); conn.execute("DELETE FROM target_tweets WHERE id=?", (tt,)); conn.commit()


async def test_queue_legend(user: User):
    _pages()
    await user.open("/queue")
    await user.should_see("卡片上的标签是什么意思？")
    await user.should_see("/280 单位")          # 免费账号按 280 单位计


async def test_queue_shows_target_tweet_metrics(user: User):
    """任务队列里引用的原推文，要带上和「抓取记录」一样的相关性 / 语言 / 观看量小标签。"""
    _pages()
    with get_conn() as c:
        c.execute("UPDATE target_tweets SET view_count=15000, llm_relevance_score=8 WHERE id=1")
        c.commit()
    await user.open("/queue")
    await user.should_see("👁 1.5万")
    await user.should_see("相关性 8/10")
    await user.should_see("发推于")
    await user.open("/targets")
    await user.should_see("👁 1.5万")
    await user.should_see("相关性 8/10")


async def test_tag_legends(user: User):
    _pages()
    await user.open("/targets")
    await user.should_see("标签颜色：")
    await user.should_see("已进任务队列")
    # 颜色真的能生效：badge 不能带 Quasar 的 color 属性（它会加 !important 的主题蓝把 Tailwind 类压掉）
    for b in [e for e in user.find(kind=ui.badge).elements]:
        assert "color" not in b._props, (b.text, b._props)
    st = [e for e in user.find("已进任务队列").elements if isinstance(e, ui.badge)][0]
    assert "bg-green-600" in st._classes, st._classes
    await user.open("/queue")
    await user.should_see("标签颜色：")
    await user.should_see("来源：AI 匹配素材")


async def test_account_dialog_has_premium_switch(user: User):
    _pages()
    await user.open("/settings")
    await user.should_see("免费账号 · 280 单位")
    user.find("添加账号").click()
    await user.should_see("已订阅 X Premium（会员）")


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


async def test_settings_media_storage_panel(user: User):
    """设置 → 数据：能看到素材附件占用统计、孤儿文件列表和「前往清理」按钮；视频单文件上限为 512MB。"""
    _pages()
    # 造一个没人引用的孤儿文件
    orphan = media.abs_path(media.new_rel_path("z.mp4"))
    orphan.parent.mkdir(parents=True, exist_ok=True); orphan.write_bytes(b"0" * 2048)
    st = media.storage_stats()
    assert st["count"] >= 2 and any(rel.endswith(".mp4") for rel, _ in st["orphans"]), st
    assert media.VIDEO_MAX_BYTES == 512 * 1024 * 1024   # 与 X 官方 API 单个视频上限一致
    assert media.check_one("big.mp4", 500 * 1024 * 1024) == ""
    assert "512MB" in media.check_one("huge.mp4", 600 * 1024 * 1024)
    await user.open("/settings")
    user.find("数据").click()
    await user.should_see("素材附件占用空间")
    await user.should_see("前往清理（打开目录）")
    await user.should_see("已没有任何素材 / 条目引用")


async def test_rule_dialog_read_media_switch(user: User):
    """规则弹窗：「读取附图打分」默认关、说明里点明成本与多模态要求；打开时弹警告；保存后落库；抓取记录页显示附件标签。"""
    _pages()
    await user.open("/rules")
    _click_button(user, "新建规则")
    await user.should_see("读取附图 / 视频封面一起打分")
    await user.should_see("token 消耗明显增加")
    sw = [e for e in user.find(kind=ui.switch).elements if e.text == "读取附图 / 视频封面一起打分"][0]
    assert sw.value is False
    UserInteraction(user, {sw}, None).trigger("update:modelValue", True)
    await user.should_see("必须是支持图片输入的多模态模型")
    [e for e in user.find("规则名").elements if isinstance(e, ui.input)][0].set_value("带图规则")
    # user.find(...).elements 是集合、顺序不定，要按标签找
    [e for e in user.find("关键词（逗号隔开").elements if isinstance(e, ui.textarea)][0].set_value("kw")
    [e for e in user.find("语义筛选条件").elements if isinstance(e, ui.textarea)][0].set_value("找人")
    _click_button(user, "保存")
    await user.should_see("已保存")
    await user.should_see("🖼 读附图打分")
    with get_conn() as c:
        assert c.execute("SELECT read_media FROM search_rules WHERE name='带图规则'").fetchone()["read_media"] == 1
        c.execute("UPDATE target_tweets SET media=? WHERE id=1",
                  ('[{"kind":"photo","preview_url":"u"},{"kind":"video","preview_url":"u","duration_ms":42000}]',))
        c.commit()
    await user.open("/targets")
    await user.should_see("🖼 1")
    await user.should_see("🎬 0:42")


async def test_settings_read_pool_panel_and_dashboard(user: User):
    """设置 → 抓取：抓取账号池面板——官方号参与开关默认关、切换即落库；限额 / 暂停分钟 / 随机间隔可改。仪表盘按账号显示窗口用量与暂停状态。"""
    _pages()
    await user.open("/settings")
    user.find("抓取").click()
    await user.should_see("抓取账号池（监控 / 搜索用哪个账号去读、限额与 429 冷却）")
    await user.should_see("每个小号每 15 分钟最多请求次数")
    await user.should_see("遇到限额后停多少分钟再继续")
    sw = [e for e in user.find(kind=ui.switch).elements if e.text.startswith("官方 API 也参与抓取")][0]
    assert sw.value is False
    UserInteraction(user, {sw}, None).trigger("update:modelValue", True)
    await user.should_see("已参与抓取（计费）")
    with get_conn() as c:
        assert c.execute("SELECT value FROM app_settings WHERE key='read_official_enabled'").fetchone()["value"] == "1"
        c.execute("UPDATE accounts SET read_paused_until='2099-01-01T00:00:00Z' WHERE handle='small1'")
        c.execute("INSERT INTO app_settings(key,value) VALUES ('monitor_resume_from_user_id','1') ON CONFLICT(key) DO UPDATE SET value='1'")
        c.execute("INSERT INTO app_settings(key,value) VALUES ('monitor_resume_at','2099-01-01T00:00:00Z') ON CONFLICT(key) DO UPDATE SET value='2099-01-01T00:00:00Z'")
        c.commit()
    await user.open("/")
    await user.should_see("抓取账号池与读额度")
    await user.should_see("小号 @small1：撞过 429，暂停到")
    await user.should_see("官方号 @acc1：最近 15 分钟 0/5 次")
    await user.should_see("监控上次因限流暂停")
    with get_conn() as c:
        c.execute("UPDATE accounts SET read_paused_until=NULL"); c.execute("UPDATE app_settings SET value='' WHERE key LIKE 'monitor_resume%'")
        c.execute("UPDATE app_settings SET value='0' WHERE key='read_official_enabled'"); c.commit()


async def test_queue_orders_by_status_time_desc(user: User):
    """任务队列各状态按自己的时间倒序：待审核按创建时间、已发送按发送时间、已跳过按决定时间，最新在前。"""
    from x_operator.ui.queue import _load
    with get_conn() as c:
        c.execute("DELETE FROM review_queue WHERE final_text LIKE 'ord_%'")
        rows = [  # (状态, 文案, 创建, 决定, 发送)
            ("pending", "ord_p_old", "2026-01-01T00:00:00Z", None, None),
            ("pending", "ord_p_new", "2026-02-01T00:00:00Z", None, None),
            ("sent", "ord_s_late", "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z", "2026-03-01T00:00:00Z"),   # 创建最早、发送最晚
            ("sent", "ord_s_early", "2026-01-05T00:00:00Z", "2026-01-06T00:00:00Z", "2026-01-07T00:00:00Z"),
            ("skipped", "ord_k_old", "2026-01-09T00:00:00Z", "2026-01-10T00:00:00Z", None),
            ("skipped", "ord_k_new", "2026-01-01T00:00:00Z", "2026-02-10T00:00:00Z", None),
        ]
        for st, txt, ca, da, sa in rows:
            c.execute("INSERT INTO review_queue(account_id, action_type, target_tweet_id, material_id, final_text, status, created_at, decided_at, sent_at) "
                      "VALUES (1,'reply',1,1,?,?,?,?,?)", (txt, st, ca, da, sa))
        c.commit()
    order = lambda st: [r["final_text"] for r in _load(st) if r["final_text"].startswith("ord_")]  # noqa: E731
    assert order("pending") == ["ord_p_new", "ord_p_old"], order("pending")
    assert order("sent") == ["ord_s_late", "ord_s_early"], order("sent")
    assert order("skipped") == ["ord_k_new", "ord_k_old"], order("skipped")
    with get_conn() as c:
        c.execute("DELETE FROM review_queue WHERE final_text LIKE 'ord_%'"); c.commit()


async def test_queue_failed_restore_button(user: User):
    """任务队列「失败」筛选：每条有「捞回待审核」按钮，点了回到待审核并显示上次失败原因；页面标题已改为任务队列。"""
    _pages()
    with get_conn() as c:
        c.execute("DELETE FROM review_queue WHERE final_text='rf_text'")
        c.execute("INSERT INTO review_queue(account_id, action_type, target_tweet_id, material_id, final_text, status, error_msg, created_at, decided_at) "
                  "VALUES (1,'reply',1,1,'rf_text','failed','X 返回 403',?,?)", (utcnow_iso(), utcnow_iso()))
        c.commit()
    await user.open("/queue")
    await user.should_see("任务队列")
    status_sel = [e for e in user.find(kind=ui.select).elements if "failed" in e.options][0]
    UserInteraction(user, {status_sel}, None).trigger("update:modelValue", {"value": list(status_sel.options).index("failed")})
    await user.should_see("发送失败 · X 返回 403")
    _click_button(user, "捞回待审核")
    await user.should_see("已捞回待审核")
    with get_conn() as c:
        row = c.execute("SELECT status, error_msg FROM review_queue WHERE final_text='rf_text'").fetchone()
        assert row["status"] == "pending" and row["error_msg"] == "X 返回 403", dict(row)
    await user.open("/queue")
    await user.should_see("上次发送失败：X 返回 403")
    with get_conn() as c:
        c.execute("DELETE FROM review_queue WHERE final_text='rf_text'"); c.commit()


async def test_settings_dispatch_interval_and_rule_auto_approve(user: User):
    """设置 → 自动运行：发送分发检查间隔可改（随「保存节奏设置」落库）；全局免审核已不存在。
    规则弹窗：免审核开关 + 阈值在「回复方式」区，默认关，打开弹警告，保存落库，卡片显示标签。"""
    _pages()
    await user.open("/settings")
    user.find("自动运行").click()
    await user.should_see("每隔多少秒检查一次待发送")
    assert not [e for e in user.find(kind=ui.switch).elements if e.text.startswith("开启免审核")]
    secs = [e for e in user.find("每隔多少秒检查一次待发送").elements if isinstance(e, ui.number)][0]
    secs.set_value(45)
    _click_button(user, "保存节奏设置")
    await user.should_see("已保存")
    with get_conn() as c:
        assert c.execute("SELECT value FROM app_settings WHERE key='dispatch_interval_seconds'").fetchone()["value"] == "45"
        c.execute("UPDATE app_settings SET value='60' WHERE key='dispatch_interval_seconds'"); c.commit()
    await user.open("/rules")
    _click_button(user, "新建规则")
    await user.should_see("免审核：置信度达标直接进待发送")
    sw = [e for e in user.find(kind=ui.switch).elements if e.text.startswith("免审核")][0]
    assert sw.value is False
    UserInteraction(user, {sw}, None).trigger("update:modelValue", True)
    await user.should_see("不经人看直接进待发送")
    thr = [e for e in user.find("置信度阈值").elements if isinstance(e, ui.number)][0]
    thr.set_value(0.8)
    [e for e in user.find("规则名").elements if isinstance(e, ui.input)][0].set_value("免审规则")
    [e for e in user.find("关键词（逗号隔开").elements if isinstance(e, ui.textarea)][0].set_value("kw")
    [e for e in user.find("语义筛选条件").elements if isinstance(e, ui.textarea)][0].set_value("找人")
    _click_button(user, "保存")
    await user.should_see("已保存")
    await user.should_see("免审核 ≥ 0.80")
    with get_conn() as c:
        row = c.execute("SELECT auto_approve, auto_approve_min_confidence FROM search_rules WHERE name='免审规则'").fetchone()
        assert row["auto_approve"] == 1 and abs(row["auto_approve_min_confidence"] - 0.8) < 1e-6, dict(row)
