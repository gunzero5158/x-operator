"""推文长度限制：按 X 的「计数单位」算长度，非会员账号超限时让 AI 缩写。

X 的算法（回复和主贴一样）：
- 拉丁字母、数字、标点等（U+0000–U+10FF、U+2000–U+200D、U+2010–U+201F、U+2032–U+2037）每个算 1 个单位；
- 中文、日文、韩文、emoji 等其他字符每个算 2 个单位；
- 每个链接不管多长固定算 23 个单位——包括没写 http 的裸域名（hotube.me、example.com/xx 这种，X 会自动识别成链接）。
免费账号上限 280 单位（= 140 个汉字/假名，或 280 个英文字母）；订阅 X Premium 的账号上限 25000。
"""
from __future__ import annotations

import re
import sqlite3

FREE_LIMIT = 280
PREMIUM_LIMIT = 25000
URL_WEIGHT = 23

# X 会把「裸域名 + 常见顶级域」自动识别成链接（twitter-text 的规则），也按 23 算；@handle、邮箱里的域名不算
_TLDS = ("com|net|org|me|io|co|jp|cn|ai|app|dev|xyz|tv|cc|info|biz|us|uk|de|fr|ru|in|it|es|nl|br|au|ca|kr|tw|hk|sg|"
         "link|site|online|shop|store|tech|ly|to|gg|fm|am|be|ch|se|no|dk|fi|pl|eu|asia|tokyo|top|club|vip|live|pro|one|"
         "art|blog|cloud|design|digital|email|games|group|life|media|news|page|space|studio|team|today|video|wiki|work|"
         "world|zone|edu|gov|mil|int|id|my|ph|th|vn|mx|ar|cl|za|ie|at|cz|pt|gr|tr|il|ae|sa|nz|moe|fun|cool|icu|bio|"
         "codes|tools|run|sh|ws|is|so|st|re|kim|hu|ro|ua|sk|bg|hr|lt|lv|ee|by|kz|lol|wtf|inc|ltd|llc")
_URL_RE = re.compile(
    r"https?://\S+"
    r"|(?<![\w@.\-/])(?:[a-z0-9](?:[a-z0-9\-]*[a-z0-9])?\.)+(?:" + _TLDS + r")(?![\w.\-@])(?:/[^\s]*)?",
    re.IGNORECASE)
_LIGHT_RANGES = ((0x0000, 0x10FF), (0x2000, 0x200D), (0x2010, 0x201F), (0x2032, 0x2037))


def _weight(ch: str) -> int:
    o = ord(ch)
    return 1 if any(lo <= o <= hi for lo, hi in _LIGHT_RANGES) else 2


def weighted_len(text: str) -> int:
    """按 X 的规则算长度（计数单位）。"""
    text = text or ""
    n = 0
    pos = 0
    for m in _URL_RE.finditer(text):
        n += sum(_weight(c) for c in text[pos:m.start()]) + URL_WEIGHT
        pos = m.end()
    n += sum(_weight(c) for c in text[pos:])
    return n


def _get(account, key: str, default=None):
    if account is None:
        return default
    try:
        return account[key]
    except (KeyError, IndexError, TypeError):
        return default


def is_premium(account) -> bool:
    return bool(_get(account, "is_premium", 0))


def limit_for(account) -> int:
    return PREMIUM_LIMIT if is_premium(account) else FREE_LIMIT


def over_by(text: str, account) -> int:
    """超出账号上限多少单位；0 = 没超。"""
    return max(0, weighted_len(text) - limit_for(account))


def describe_limit(account) -> str:
    if is_premium(account):
        return f"会员账号，上限 {PREMIUM_LIMIT} 单位"
    return f"免费账号，上限 {FREE_LIMIT} 单位（≈140 个汉字/假名，链接固定算 23）"


def over_message(text: str, account) -> str:
    """给人看的超限说明。"""
    n = weighted_len(text)
    return (f"正文 {n} 单位，超过账号 @{_get(account, 'handle', '?')} 的上限 {limit_for(account)}"
            f"（中日韩每字算 2、链接算 23；未订阅 Premium 的账号最多 280）")


def fit(text: str, account, llm, must_include: list[str] | None = None, lang: str = "") -> tuple[str, str]:
    """生成内容的统一出口：不超限原样返回；超限且配了 LLM 就让 AI 缩写（保留必带项）；缩不下来或没 LLM 就原样返回并在说明里写明。
    返回 (正文, 说明)。说明为空表示没动过。"""
    text = (text or "").strip()
    over = over_by(text, account)
    if not over:
        return text, ""
    limit = limit_for(account)
    n = weighted_len(text)
    if llm is None or not llm.configured:
        return text, f"⚠ 正文 {n} 单位，超过免费账号上限 {limit}（≈140 个汉字），没配 LLM 无法自动缩写，请手动删减或换成 Premium 账号发"
    try:
        res = llm.shorten(text, lang or "und", limit, list(must_include or []))
    except Exception as e:  # LLMError 及其子类
        return text, f"⚠ 正文 {n} 单位，超过免费账号上限 {limit}，AI 缩写失败（{str(e)[:100]}），请手动删减"
    new = (res.get("text") or "").strip()
    if new and weighted_len(new) <= limit:
        return new, f"原文 {n} 单位超过上限 {limit}，已让 AI 缩写到 {weighted_len(new)} 单位"
    return text, f"⚠ 正文 {n} 单位，超过免费账号上限 {limit}，AI 两次都没缩到上限内，请手动删减"
