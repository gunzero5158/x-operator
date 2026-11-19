import unicodedata
from x_operator.core.langdetect import detect, LANG_LABEL, lang_allowed, lang_for_llm, material_lang_tiers
from x_operator.core.search import effective_query
from x_operator.ui.i18n import detect_language

def test_vietnamese():
    text = "Tôi đang tìm một dịch vụ tốt cho đội ngũ"
    assert detect(text) == "vi"
    assert detect(unicodedata.normalize("NFD", text)) == "vi"
    assert LANG_LABEL["vi"] == "越南语"
    assert lang_allowed("vi", ["vi"])
    assert not lang_allowed("vi", ["en"])
    assert material_lang_tiers("vi") == (["vi"], [])
    assert "越南语" in lang_for_llm("vi")
    assert "lang:vi" in effective_query({"keyword_query": "dịch vụ", "lang": "vi"}, access_type="official")
    assert detect_language("ru-RU, en;q=0.8") == "ru"
