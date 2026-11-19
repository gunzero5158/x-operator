from x_operator.core.langdetect import detect, LANG_LABEL, lang_for_llm, lang_allowed, material_lang_tiers, refine_tweet_lang
from x_operator.core.search import effective_query
from x_operator.llm import prompts

def test_russian_content_pipeline():
    assert LANG_LABEL['ru'] == '俄语'
    for text in ['Привет, мир!', 'Ищу быстрый API для нашей команды', 'Ёжик https://example.com @user']:
        assert detect(text) == 'ru'
    assert detect('123 https://example.com @user') == ''
    assert detect('Це український текст') != 'ru'
    assert refine_tweet_lang('ru', 'Привет') == 'ru'
    assert lang_allowed('ru', ['ru'])
    assert not lang_allowed('ru', ['en'])
    assert lang_allowed('ru', [])
    assert material_lang_tiers('ru') == (['ru'], [])
    assert '俄语' in lang_for_llm('ru')
    rule = {'keyword_query': 'технологии', 'lang': 'ru'}
    assert 'lang:ru' in effective_query(rule, access_type='official')
    rule = {'keyword_query': 'Claude, Клод', 'lang': 'ru'}
    assert effective_query(rule, access_type='unofficial') == '(Claude OR Клод) lang:ru -is:retweet'
    assert '俄语' in prompts.write_user('Привет', 'ru', '', [])
    assert '俄语' in prompts.post_write_user('Новости', 'ru', [], [])
    assert '俄语' in prompts.material_gen_user('reply', 'ru', 'Новости', '', '', [], 1)


def test_cookie_search_only_adds_one_x_language_and_preserves_explicit_queries():
    assert effective_query({'keyword_query': 'Claude', 'lang': 'ru,ja'}, access_type='unofficial') == 'Claude -is:retweet'
    assert effective_query({'keyword_query': 'Claude', 'lang': 'zh-Hans,zh-Hant'}, access_type='unofficial') == 'Claude lang:zh -is:retweet'
    assert effective_query({'keyword_query': 'Claude lang:ru', 'lang': 'ru'}, access_type='unofficial') == 'Claude lang:ru -is:retweet'
    assert effective_query({'keyword_query': 'Claude', 'lang': ''}, access_type='unofficial') == 'Claude -is:retweet'
