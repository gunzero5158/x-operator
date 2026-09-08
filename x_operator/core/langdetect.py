"""素材正文的语言自动判断（不联网、不依赖第三方库）。

思路：先看字符集——假名 → ja、谚文 → ko、泰文 → th、只有汉字没有假名 → zh；
全是拉丁字母时按常用功能词投票区分 en/es/fr/de/pt/id，投不出来就按 en。
判不出来（空文本、纯表情/数字/链接）返回 ""，由界面让用户手选托底。
"""
from __future__ import annotations

import re

# 语言码 → 中文名（规则页 / 素材页 / 抓取记录共用）
LANG_LABEL = {"ja": "日语", "en": "英语", "zh": "中文", "ko": "韩语", "es": "西班牙语",
              "fr": "法语", "de": "德语", "pt": "葡萄牙语", "id": "印尼语", "th": "泰语"}

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


def detect(text: str) -> str:
    """返回 ISO 639-1 语言码（ja/en/zh/ko/th/es/fr/de/pt/id），判不出来返回 ""。"""
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
        # 只有汉字没有假名：中文。（日文全汉字短句会误判成 zh，属于可接受的手选托底场景）
        return "zh"

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
