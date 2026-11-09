# START OF FILE tests/test_qa_v2.py

"""
Тесты Q&A v2 (эксперимент /qa2): сборщик подборки статей (services/qa_context.py)
и цикл search_laws (services/qa_v2_service.py).

Ноги поиска/БД/Vertex мокируются: тесты гоняют чистую логику merge/дедуп/потолок/цикл.
Полный цикл ask_question_v2 требует google-genai (types) — под importorskip.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import config
from services import qa_context
from services import qa_v2_service
from services.qa_v2_service import (
    QaV2EmptyContext,
    _extract_function_calls,
    _make_thinking_config,
    _run_tool_search,
)
from prompts_qa_v2 import QA_V2_PROMPT_VERSION


def _run(coro):
    return asyncio.run(coro)


def _art(key, text='текст статьи', **kw):
    base = {
        'key': key,
        'doc_title': kw.get('doc_title', 'Уголовный кодекс'),
        'article_ref': kw.get('article_ref', str(key[1])),
        'title': kw.get('title', 'Название'),
        'penalty': kw.get('penalty', ''),
        'text': text,
    }
    return base


# --- _norm_ref -----------------------------------------------------------------

def test_norm_ref_variants_match():
    assert qa_context._norm_ref('Статья 12.1') == '12.1'
    assert qa_context._norm_ref('12.1.') == '12.1'
    assert qa_context._norm_ref('  статья 12.1  ') == '12.1'
    assert qa_context._norm_ref('12.1') == qa_context._norm_ref('Статья 12.1')


def test_norm_ref_empty_falls_back_to_title():
    assert qa_context._norm_ref('', 'Преамбула') == 'преамбула'
    assert qa_context._norm_ref(None, None) == ''


@pytest.mark.parametrize(
    ('raw', 'expected'),
    [
        ('12.1', 'Статья 12.1'),
        ('12-1. Название', 'Статья 12-1. Название'),
        ('Статья 12.1.', 'Статья 12.1.'),
        ('Глава IV', 'Глава IV'),
        ('Комментарий I', 'Комментарий I'),
        ('Толкование Верховного суда № 4', 'Толкование Верховного суда № 4'),
        ('Прецедент Smith v. State', 'Прецедент Smith v. State'),
        ('Приложение', 'Приложение'),
    ],
)
def test_display_ref_does_not_duplicate_structural_prefix(raw, expected):
    assert qa_context._display_ref(raw) == expected


# --- format_articles_block -----------------------------------------------------

def test_format_block_numbering_penalty_and_markers():
    arts = [
        _art((1, '1.1'), penalty='штраф 5000'),
        _art((1, '1.2')),
    ]
    text, used, dropped = qa_context.format_articles_block(arts, max_chars=100_000)
    assert '[1] ' in text and '[2] ' in text
    assert 'Санкция: штраф 5000' in text
    assert text.startswith('=== ПОДБОРКА СТАТЕЙ')
    assert text.endswith('=== КОНЕЦ ПОДБОРКИ ===')
    assert len(used) == 2 and dropped == 0


def test_format_block_cap_drops_tail_keeps_head():
    arts = [_art((1, str(i)), text='х' * 400) for i in range(10)]
    text, used, dropped = qa_context.format_articles_block(arts, max_chars=1500)
    assert 0 < len(used) < 10
    assert dropped == 10 - len(used)
    # Первая статья всегда входит (потолок не может выкинуть всё).
    assert '[1] ' in text


# --- collect_articles ----------------------------------------------------------

def test_collect_merge_dedup_and_exclude(monkeypatch):
    monkeypatch.setattr(config, 'QA_V2_NEIGHBOR_RADIUS', 0)
    catalog = [_art((1, '1.1')), _art((1, '2.5'))]
    vector = [
        _art((1, '1.1'), title=''),      # дубль каталожной — должен отсеяться
        _art((2, '7.3'), title=''),      # уникальная векторная
    ]

    async def fake_catalog(server_id, query, limit):
        return list(catalog)

    async def fake_vector(server_id, query, top_k):
        return list(vector)

    monkeypatch.setattr(qa_context, '_catalog_leg', fake_catalog)
    monkeypatch.setattr(qa_context, '_vector_leg', fake_vector)

    arts, legs = _run(qa_context.collect_articles('srv', 'вопрос'))
    keys = [a['key'] for a in arts]
    assert keys == [(1, '1.1'), (1, '2.5'), (2, '7.3')]  # каталог первым, дубль убран
    assert legs == {'catalog': 'ok', 'vector': 'ok'}

    # exclude_keys: уже переданные статьи не возвращаются (раунды search_laws).
    arts2, _ = _run(qa_context.collect_articles(
        'srv', 'вопрос', exclude_keys={(1, '1.1'), (2, '7.3')}
    ))
    assert [a['key'] for a in arts2] == [(1, '2.5')]


def test_collect_neighbors_appended_after_legs_and_deduped(monkeypatch):
    """Соседи документа идут ПОСЛЕ обеих ног и дедупятся с ними."""
    monkeypatch.setattr(config, 'QA_V2_NEIGHBOR_RADIUS', 2)

    async def fake_catalog(server_id, query, limit):
        return [dict(_art((1, '17')), id=100)]

    async def fake_vector(server_id, query, top_k):
        return [_art((1, '20'), title='')]

    async def fake_neighbors(server_id, items, radius, top_n):
        assert [a['id'] for a in items] == [100]
        return [dict(_art((1, '16')), id=99), dict(_art((1, '17')), id=100)]  # 17 = дубль

    monkeypatch.setattr(qa_context, '_catalog_leg', fake_catalog)
    monkeypatch.setattr(qa_context, '_vector_leg', fake_vector)
    monkeypatch.setattr(qa_context, '_expand_neighbors', fake_neighbors)

    arts, legs = _run(qa_context.collect_articles('srv', 'вопрос'))
    assert [a['key'] for a in arts] == [(1, '17'), (1, '20'), (1, '16')]
    assert legs['neighbors'] == 'ok'


def test_expand_neighbors_orders_by_source_order(monkeypatch):
    captured = {}

    async def fake_fetch(sql, *args):
        captured['sql'] = sql
        captured['args'] = args
        return []

    monkeypatch.setattr(qa_context.db_manager, 'fetch', fake_fetch)
    result = _run(qa_context._expand_neighbors(
        'srv',
        [dict(_art((1, '17')), id=100)],
        radius=2,
        top_n=12,
    ))

    assert result == []
    compact_sql = ' '.join(captured['sql'].split())
    assert 'ORDER BY source_order, id' in compact_sql
    assert 'ORDER BY law_document_id, rn, id' in compact_sql
    assert captured['args'] == ('srv', [100], 2)


def test_collect_one_leg_down_other_survives(monkeypatch):
    async def broken_catalog(server_id, query, limit):
        raise RuntimeError('поиск лёг')

    async def fake_vector(server_id, query, top_k):
        return [_art((3, '4.4'), title='')]

    monkeypatch.setattr(qa_context, '_catalog_leg', broken_catalog)
    monkeypatch.setattr(qa_context, '_vector_leg', fake_vector)

    arts, legs = _run(qa_context.collect_articles('srv', 'вопрос'))
    assert [a['key'] for a in arts] == [(3, '4.4')]
    assert legs == {'catalog': 'error', 'vector': 'ok'}


def test_build_context_none_when_empty(monkeypatch):
    async def empty(*args, **kw):
        return []

    monkeypatch.setattr(qa_context, '_catalog_leg', empty)
    monkeypatch.setattr(qa_context, '_vector_leg', empty)
    assert _run(qa_context.build_qa_context('srv', 'вопрос')) is None


# --- qa_v2_service: хелперы ----------------------------------------------------

def test_extract_function_calls_from_parts():
    fc = SimpleNamespace(name='search_laws', args={'query': 'x'})
    resp = SimpleNamespace(
        function_calls=None,
        candidates=[SimpleNamespace(content=SimpleNamespace(parts=[
            SimpleNamespace(function_call=None),
            SimpleNamespace(function_call=fc),
        ]))],
    )
    assert _extract_function_calls(resp) == [fc]
    assert _extract_function_calls(SimpleNamespace(function_calls=None, candidates=[])) == []


def test_make_thinking_config_levels_and_off():
    class FakeTypes:
        class ThinkingLevel:
            MINIMAL, LOW, MEDIUM, HIGH = 'MIN', 'LOW', 'MED', 'HIGH'

        class ThinkingConfig:
            def __init__(self, thinking_level):
                self.thinking_level = thinking_level

    cfg = _make_thinking_config(FakeTypes, 'medium')
    assert cfg.thinking_level == 'MED'
    assert _make_thinking_config(FakeTypes, '') is None       # пусто = откат, не слать
    assert _make_thinking_config(FakeTypes, 'мусор') is None  # незнакомый уровень = не слать


def test_run_tool_search_dedups_queries_and_updates_keys(monkeypatch):
    calls = []

    async def fake_build(server_id, query, **kw):
        calls.append(query)
        return {'text': 'ДОП СТАТЬИ', 'keys': {(9, '9.9')}, 'count': 1, 'chars': 10,
                'dropped': 0, 'articles': [], 'legs': {'catalog': 'ok', 'vector': 'empty'}}

    monkeypatch.setattr(qa_v2_service, 'build_qa_context', fake_build)
    seen_keys, seen_queries = set(), set()

    r1 = _run(_run_tool_search('srv', 'Порядок обжалования', seen_keys, seen_queries))
    assert r1['added'] == 1 and (9, '9.9') in seen_keys

    # Повтор того же запроса (другим регистром) — поиск НЕ дёргается, модели «пусто».
    r2 = _run(_run_tool_search('srv', 'порядок обжалования', seen_keys, seen_queries))
    assert r2['added'] == 0 and 'Ничего нового' in r2['text']
    assert calls == ['Порядок обжалования']

    r3 = _run(_run_tool_search('srv', '', seen_keys, seen_queries))
    assert r3['added'] == 0 and calls == ['Порядок обжалования']


# --- qa_v2_service: полный цикл (нужен google-genai — importorskip ВНУТРИ тестов,
# чтобы без SDK скипались только они, а не весь модуль) --------------------------

def _fake_response_with_call(query):
    types_mod = pytest.importorskip('google.genai').types
    return SimpleNamespace(
        function_calls=[SimpleNamespace(name='search_laws', args={'query': query})],
        candidates=[SimpleNamespace(content=types_mod.Content(
            role='model', parts=[types_mod.Part(text='(думаю)')],
        ))],
        text=None,
    )


_FINAL = SimpleNamespace(function_calls=None, candidates=[], text='<b>Ответ готов</b>')


def _wire_common(monkeypatch, responses):
    """Мокирует Vertex-обвязку: execute_vertex_request отдаёт ответы по списку."""
    import rate_limiter
    import utils

    seq = list(responses)

    async def fake_execute(func=None, **kw):
        return seq.pop(0)

    async def fake_reinit():
        return True

    monkeypatch.setattr(rate_limiter, 'execute_vertex_request', fake_execute)
    monkeypatch.setattr(utils, 'reinitialize_vertex_ai', fake_reinit)
    monkeypatch.setitem(config.SERVERS_CONFIG, 'test_v2_srv', {'display_name': 'Test'})

    async def fake_build(server_id, query, **kw):
        if kw.get('exclude_keys'):
            return {'text': 'ДОП', 'keys': {(2, '2.2')}, 'count': 2, 'chars': 20,
                    'dropped': 0, 'articles': [], 'legs': {'catalog': 'ok', 'vector': 'empty'}}
        return {'text': 'ПОДБОРКА', 'keys': {(1, '1.1')}, 'count': 5, 'chars': 100,
                'dropped': 0, 'articles': [], 'legs': {'catalog': 'ok', 'vector': 'ok'}}

    monkeypatch.setattr(qa_v2_service, 'build_qa_context', fake_build)


def test_full_loop_one_tool_round(monkeypatch):
    monkeypatch.setattr(config, 'QA_V2_TOOL_MAX_ROUNDS', 1)
    _wire_common(monkeypatch, [_fake_response_with_call('порядок обжалования'), _FINAL])

    result = _run(qa_v2_service.ask_question_v2('test_v2_srv', 'вопрос'))
    assert result['answer'] == '<b>Ответ готов</b>'
    d = result['diag']
    assert d['model_calls'] == 2 and d['rounds_used'] == 1
    assert d['prompt_version'] == QA_V2_PROMPT_VERSION
    assert d['tool_rounds'] == [{'query': 'порядок обжалования', 'added': 2}]
    assert d['initial_count'] == 5 and d['total_added'] == 2


def test_full_loop_runaway_function_calls_terminates(monkeypatch):
    """Предохранитель: модель зовёт поиск бесконечно → цикл конечен, честный RuntimeError."""
    monkeypatch.setattr(config, 'QA_V2_TOOL_MAX_ROUNDS', 1)
    endless = [_fake_response_with_call(f'q{i}') for i in range(10)]
    _wire_common(monkeypatch, endless)

    with pytest.raises(RuntimeError):
        _run(qa_v2_service.ask_question_v2('test_v2_srv', 'вопрос'))


def test_full_loop_never_executes_tool_call_after_round_limit(monkeypatch):
    monkeypatch.setattr(config, 'QA_V2_TOOL_MAX_ROUNDS', 1)
    _wire_common(monkeypatch, [
        _fake_response_with_call('разрешённый раунд'),
        _fake_response_with_call('запрещённый лишний раунд'),
    ])
    calls = []

    async def fake_tool(server_id, raw_query, seen_keys, seen_queries):
        calls.append(raw_query)
        return {'text': 'ДОП', 'added': 1, 'query': raw_query}

    monkeypatch.setattr(qa_v2_service, '_run_tool_search', fake_tool)

    with pytest.raises(RuntimeError):
        _run(qa_v2_service.ask_question_v2('test_v2_srv', 'вопрос'))

    assert calls == ['разрешённый раунд']


def test_empty_context_raises(monkeypatch):
    pytest.importorskip('google.genai')  # ask_question_v2 лениво импортирует types
    _wire_common(monkeypatch, [_FINAL])

    async def none_build(server_id, query, **kw):
        return None

    monkeypatch.setattr(qa_v2_service, 'build_qa_context', none_build)
    with pytest.raises(QaV2EmptyContext):
        _run(qa_v2_service.ask_question_v2('test_v2_srv', 'вопрос'))

# END OF FILE tests/test_qa_v2.py
