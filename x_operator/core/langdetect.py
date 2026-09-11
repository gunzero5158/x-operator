"""语言自动判断（不联网、不依赖第三方库）。

思路：先看字符集——假名 → ja、谚文 → ko、泰文 → th、只有汉字没有假名 → 中文；
中文再按「简繁写法不同的常用字」各出现几次分成简体 zh-Hans / 繁体 zh-Hant；
全是拉丁字母时按常用功能词投票区分 en/es/fr/de/pt/id，投不出来就按 en。
判不出来（空文本、纯表情/数字/链接）返回 ""，由界面让用户手选托底。

中文的语言码：zh-Hans = 简体、zh-Hant = 繁体；单独的 zh 只出现在旧数据和「看不出简繁」的推文上，
表示「中文，简繁未定」——筛选和匹配素材时简繁两边都算它。X 搜索只认 lang:zh，简繁由本地判断再筛。
"""
from __future__ import annotations

import re

ZH_HANS, ZH_HANT, ZH_ANY = "zh-Hans", "zh-Hant", "zh"

# 可选的语言码 → 中文名（规则页 / 素材页 / 定时发帖 / AI 生成素材的下拉都用它）
LANG_LABEL = {"ja": "日语", "en": "英语", ZH_HANS: "简体中文", ZH_HANT: "繁体中文", "ko": "韩语", "es": "西班牙语",
              "fr": "法语", "de": "德语", "pt": "葡萄牙语", "id": "印尼语", "th": "泰语"}
# 只用于显示、不能手选的
_EXTRA_NAME = {ZH_ANY: "中文（简繁未定）", "und": "未知", "": "未知"}

# 简繁写法不同的常用字，两两对应（前简后繁）。只收「简体字不会出现在繁体文里、繁体字也不会出现在简体文里」的，
# 后/里/发髮这类一对多或两边都用的字不收，免得误判
_ZH_PAIRS = (
    "这這 们們 个個 来來 时時 为為 会會 说說 对對 国國 过過 还還 没沒 发發 现現 样樣 经經 开開 关關 问問 题題 学學 长長 东東 "
    "车車 书書 见見 听聽 买買 卖賣 钱錢 电電 话話 语語 气氣 边邊 点點 实實 应應 该該 从從 让讓 给給 业業 务務 产產 场場 动動 "
    "机機 线線 网網 页頁 视視 频頻 图圖 华華 爱愛 欢歡 乐樂 岁歲 帮幫 请請 谢謝 认認 识識 读讀 写寫 办辦 员員 历歷 万萬 与與 "
    "专專 两兩 严嚴 临臨 丽麗 举舉 么麼 义義 乱亂 争爭 亚亞 亲親 亿億 仅僅 价價 众眾 优優 伟偉 传傳 伤傷 体體 儿兒 兴興 养養 "
    "内內 军軍 农農 决決 况況 净淨 击擊 刘劉 则則 刚剛 创創 删刪 别別 剧劇 剑劍 劳勞 势勢 区區 医醫 协協 单單 卫衛 却卻 厂廠 "
    "压壓 厅廳 县縣 参參 双雙 变變 号號 吗嗎 启啟 响響 园園 围圍 圆圓 圣聖 坏壞 块塊 坚堅 声聲 处處 备備 头頭 夺奪 奋奮 奖獎 "
    "妈媽 妇婦 孙孫 宁寧 宝寶 审審 宪憲 寻尋 导導 将將 尔爾 尝嘗 层層 岛島 币幣 师師 带帶 庆慶 废廢 异異 张張 弹彈 归歸 当當 "
    "录錄 忆憶 忧憂 怀懷 态態 总總 恋戀 恶惡 惊驚 惯慣 戏戲 战戰 执執 扩擴 扫掃 扬揚 护護 报報 担擔 拥擁 择擇 挥揮 损損 换換 "
    "摄攝 敌敵 数數 断斷 无無 旧舊 显顯 暂暫 术術 杀殺 杂雜 权權 条條 杨楊 极極 构構 枪槍 标標 档檔 桥橋 梦夢 检檢 楼樓 欧歐 "
    "残殘 毕畢 汉漢 汤湯 沟溝 泪淚 泽澤 洁潔 浅淺 测測 济濟 浓濃 满滿 灯燈 灵靈 灾災 热熱 爷爺 状狀 犹猶 独獨 猎獵 献獻 环環 "
    "画畫 疗療 盖蓋 监監 盘盤 码碼 础礎 确確 离離 称稱 积積 稳穩 穷窮 竞競 笔筆 签簽 简簡 类類 紧緊 红紅 约約 级級 纪紀 纯純 "
    "纸紙 练練 组組 细細 织織 终終 结結 绍紹 络絡 绝絕 统統 继繼 绩績 续續 维維 综綜 绿綠 编編 缘緣 缩縮 罗羅 罚罰 职職 联聯 "
    "肠腸 肤膚 胜勝 脑腦 脸臉 艺藝 节節 药藥 获獲 营營 蓝藍 虑慮 虽雖 补補 装裝 观觀 规規 览覽 觉覺 计計 讨討 训訓 议議 记記 "
    "讲講 许許 论論 设設 访訪 证證 评評 诉訴 词詞 试試 诗詩 诚誠 详詳 误誤 课課 谁誰 调調 谈談 贝貝 负負 贡貢 财財 责責 败敗 "
    "货貨 质質 购購 贵貴 费費 贴貼 贸貿 资資 赏賞 赛賽 赢贏 赶趕 趋趨 跃躍 践踐 轮輪 转轉 轻輕 载載 较較 辅輔 辆輛 辑輯 输輸 "
    "达達 迁遷 运運 进進 远遠 连連 迟遲 选選 递遞 逻邏 邮郵 邻鄰 郑鄭 释釋 钟鐘 钢鋼 铁鐵 银銀 链鏈 销銷 锁鎖 错錯 键鍵 门門 "
    "闭閉 闲閒 间間 闻聞 阅閱 队隊 阳陽 阴陰 阶階 际際 陆陸 陈陳 险險 随隨 隐隱 难難 雾霧 韩韓 顶頂 项項 顺順 须須 顾顧 顿頓 "
    "预預 领領 颜顏 风風 飞飛 饭飯 饮飲 饰飾 馆館 马馬 驾駕 验驗 骑騎 鱼魚 鸟鳥 鸡雞 麦麥 黄黃 齐齊 龙龍 龟龜 订訂 软軟 户戶 "
    "温溫 静靜 宫宮 强強"
).split()
_HANS_ONLY = frozenset(p[0] for p in _ZH_PAIRS)
_HANT_ONLY = frozenset(p[1] for p in _ZH_PAIRS)

# 拉丁语系的高频功能词（尽量挑互不重叠、短句里也常出现的）
_LATIN_STOPWORDS: dict[str, set[str]] = {
    "en": {"the", "and", "is", "are", "you", "for", "with", "this", "that", "of", "to", "in", "it", "on", "we", "your", "our", "not", "have", "be"},
    "es": {"el", "la", "los", "las", "es", "de", "que", "y", "en", "un", "una", "para", "con", "por", "no", "se", "su", "más", "como", "muy"},
    "fr": {"le", "la", "les", "est", "de", "que", "et", "en", "un", "une", "pour", "avec", "pas", "des", "du", "je", "vous", "nous", "sur", "très"},
    "de": {"der", "die", "das", "ist", "und", "nicht", "ich", "sie", "wir", "mit", "für", "ein", "eine", "auf", "zu", "den", "sich", "auch", "es", "im"},
    "pt": {"o", "a", "os", "as", "é", "de", "que", "e", "em", "um", "uma", "para", "com", "não", "do", "da", "você", "se", "mais", "muito"},
    "id": {"yang", "dan", "di", "ini", "itu", "untuk", "dengan", "tidak", "ada", "saya", "kami", "anda", "dari", "ke", "akan", "bisa", "juga", "sudah", "kalau", "ya"},
}

_RE_KANA = re.compile(r"[぀-ヿㇰ-ㇿ]")
_RE_HANGUL = re.compile(r"[가-힯ᄀ-ᇿ㄰-㆏]")
_RE_THAI = re.compile(r"[฀-๿]")
_RE_HAN = re.compile(r"[一-鿿㐀-䶿]")
_RE_LATIN = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ]")
_RE_WORD = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ']+")
_RE_NOISE = re.compile(r"https?://\S+|www\.\S+|[@#]\w+")


def zh_script(text: str) -> str:
    """中文正文是简体还是繁体：数简繁写法不同的常用字，多的一边赢。返回 zh-Hans / zh-Hant，
    一个都没有或打平（比如「我在日本工作」这种两边写法一样的）返回 ""。"""
    hans = hant = 0
    for ch in text or "":
        if ch in _HANS_ONLY:
            hans += 1
        elif ch in _HANT_ONLY:
            hant += 1
    if hans == hant:
        return ""
    return ZH_HANS if hans > hant else ZH_HANT


def detect(text: str) -> str:
    """返回语言码（ja/en/zh-Hans/zh-Hant/ko/th/es/fr/de/pt/id），判不出来返回 ""。
    中文看不出简繁时按简体（界面会提示可以手选繁体）。"""
    s = _RE_NOISE.sub(" ", text or "")
    if not s.strip():
        return ""
    kana = len(_RE_KANA.findall(s))
    hangul = len(_RE_HANGUL.findall(s))
    thai = len(_RE_THAI.findall(s))
    han = len(_RE_HAN.findall(s))
    latin = len(_RE_LATIN.findall(s))
    cjk_total = kana + hangul + thai + han

    # 有任何东亚/东南亚文字就优先按它判：日常推文里夹几个英文单词很常见
    if cjk_total:
        if kana:
            return "ja"
        if hangul:
            return "ko"
        if thai:
            return "th"
        # 只有汉字没有假名：中文，再分简繁。（日文全汉字短句会误判成中文，属于可接受的手选托底场景）
        return zh_script(s) or ZH_HANS

    if not latin:
        return ""  # 纯表情 / 数字 / 符号

    words = [w.lower() for w in _RE_WORD.findall(s)]
    if not words:
        return ""
    score = {lang: sum(1 for w in words if w in sw) for lang, sw in _LATIN_STOPWORDS.items()}
    best = max(score, key=score.get)
    if score[best] == 0:
        return "en"  # 全是拉丁字母但没命中功能词（品牌名、单个词）：默认英语
    # 并列时优先英语，其次按字典顺序稳定
    top = score[best]
    if score["en"] == top:
        return "en"
    return best


# ---------------- 语言码的规整、显示与比较 ----------------
_HANS_ALIASES = {"zh-hans", "zh-cn", "zh-sg", "zh-my", "zh_cn", "zh-chs", "cmn-hans"}
_HANT_ALIASES = {"zh-hant", "zh-tw", "zh-hk", "zh-mo", "zh_tw", "zh_hk", "zh-cht", "cmn-hant"}


def normalize(code: str | None) -> str:
    """各种写法的中文语言码统一成 zh-Hans / zh-Hant / zh；其他语言码原样（去空格、转小写）。"""
    c = (code or "").strip()
    low = c.lower()
    if low in _HANS_ALIASES:
        return ZH_HANS
    if low in _HANT_ALIASES:
        return ZH_HANT
    if low in ("zh", "cmn", "zho", "chi"):
        return ZH_ANY
    return low


def is_zh(code: str | None) -> bool:
    return normalize(code) in (ZH_HANS, ZH_HANT, ZH_ANY)


def refine_tweet_lang(lang: str | None, text: str) -> str | None:
    """X 给推文标的语言码 → 本工具用的：中文按正文字形细分简繁（X 的 lang 字段基本只给 zh，分不出来），
    字形看不出来时保留 X 自己的判断（zh-tw 之类）或 zh（简繁未定）。非中文原样返回。"""
    if not is_zh(lang):
        return lang
    return zh_script(text) or normalize(lang)


def lang_name(code: str | None) -> str:
    """显示用的中文名：日语 / 简体中文 / 繁体中文 / 中文（简繁未定）…… 不认识的原样。"""
    c = normalize(code) if is_zh(code) else (code or "")
    return LANG_LABEL.get(c) or _EXTRA_NAME.get(c) or c


def rule_langs_normalized(codes) -> list[str]:
    """规则语言列表规整：zh（旧数据 / AI 生成规则给的）展开成简繁两个，别名统一，去重保序。"""
    out: list[str] = []
    for x in codes or []:
        c = normalize(str(x))
        if not c:
            continue
        for y in ([ZH_HANS, ZH_HANT] if c == ZH_ANY else [c]):
            if y not in out:
                out.append(y)
    return out


def x_search_code(code: str) -> str:
    """规则语言码 → X 搜索语法里的 lang: 值。X 只认 lang:zh，简繁由本地判断后再筛。"""
    return "zh" if is_zh(code) else code


def lang_allowed(tweet_lang: str | None, allowed: list[str]) -> bool:
    """推文语言是否在规则选的语言里。没有语言 / X 标 und 等判不出的放行；
    「中文（简繁未定）」的推文只要规则选了任一种中文就放行；规则里的旧值 zh 表示简繁都要。"""
    if not allowed:
        return True
    code = normalize(tweet_lang)
    if not code or code in ("und", "qme", "zxx"):
        return True
    wanted = rule_langs_normalized(allowed)
    if code in wanted:
        return True
    return code == ZH_ANY and any(is_zh(a) for a in wanted)


def material_lang_tiers(lang: str | None) -> tuple[list[str], list[str]]:
    """给一条推文挑回复素材时的语言优先级：(同语言的素材语言码, 退而求其次的)。
    繁体推文：先繁体素材（旧的「中文」素材也算），没有再用简体；简繁未定的推文：简繁都算同语言。"""
    code = normalize(lang)
    if code == ZH_HANT:
        return [ZH_HANT, ZH_ANY], [ZH_HANS]
    if code == ZH_HANS:
        return [ZH_HANS, ZH_ANY], [ZH_HANT]
    if code == ZH_ANY:
        return [ZH_HANS, ZH_HANT, ZH_ANY], []
    return [code], []


def lang_for_llm(code: str | None) -> str:
    """写进提示词里的语言说明。中文特别写明简体还是繁体，免得模型写成另一种。"""
    c = normalize(code)
    if c == ZH_HANS:
        return "简体中文（zh-Hans），全文用简体字"
    if c == ZH_HANT:
        return "繁体中文（zh-Hant），全文用繁体字，不要混入简体字"
    if c == ZH_ANY:
        return "中文（看不出是简体还是繁体，照原文的字形写）"
    if not c or c == "und":
        return "未知（照原文用的语言）"
    return f"{LANG_LABEL.get(c, c)}（{c}）"
