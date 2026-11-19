from x_operator.core.langdetect import detect, LANG_LABEL, lang_for_llm, lang_allowed, material_lang_tiers
from x_operator.core.search import effective_query
from x_operator.llm import prompts

def test_arabic_detection_and_selection():
    assert 'ar' in LANG_LABEL
    assert detect('أبحث عن خدمة API سريعة وبسعر مناسب') == 'ar'
    assert detect('مرحبا بالعالم https://example.com @account') == 'ar'
    assert detect('١٢٣٤٥') == ''
    assert detect('The service is useful for our team') == 'en'
    assert lang_allowed('ar', ['ar'])
    assert not lang_allowed('en', ['ar'])
    assert material_lang_tiers('ar') == (['ar'], [])

def test_arabic_search_and_prompt():
    rule = {'keyword_query':'خدمة','lang':'ar'}
    assert 'lang:ar' in effective_query(rule, access_type='official')
    assert 'lang:ar' not in effective_query(rule, access_type='unofficial')
    assert '阿拉伯语' in lang_for_llm('ar')
