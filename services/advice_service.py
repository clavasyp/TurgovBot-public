# START OF FILE services/advice_service.py

"""
«Мозг» голосового оверлея (Фаза 2): по ситуации/вопросу из транскрипта даёт короткий
практичный совет по законам сервера со ссылками на статьи.

Конвейер: rewrite_queries() (flash-lite нормализует шумный транскрипт в 1–4 поисковых запроса,
fail-open) -> retrieve()/retrieve_multi() (pgvector) -> сборка контекста -> Gemini (Flash для
быстрого совета, Pro для глубокого) -> построчный enforcement (пункты с фантомными
статьями/частями/цитатами режутся) -> текст совета. Источник норм — ТОЛЬКО ретривал по
серверу (RAG), модель не выдумывает статьи.

Тестируется без аудио: generate_advice(server_id, situation_text).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import config
from rate_limiter import execute_vertex_request, RequestPriority, get_current_client, vertex_rate_limiter
from services.retrieval_service import retrieve, retrieve_multi, format_chunks_for_prompt
from law_sources_data import leadership_layers
from services.roles import get_role_prompt, normalize_mode

logger = logging.getLogger(__name__)


# --- Citation-guard: дешёвая детерминированная проверка ссылок на статьи ---
# Лукбихайнд (?<![а-яёa-z]) — чтобы «ст» не матчился ВНУТРИ слов («арест 5 суток», «возраст 18 лет»).
_ARTICLE_NUM_RE = re.compile(r'(?<![а-яёa-z])ст(?:атья|атьи|атье|\.)?\s*№?\s*(\d+(?:\.\d+)*)', re.IGNORECASE)

# Кодекс перед номером («УК ст. 17») — дизамбигуация одноимённых статей разных кодексов:
# сокращения из формата v3-промпта, сверяем с title чанка по подстроке названия документа.
_CODEX_ART_RE = re.compile(
    r'(?<![А-ЯЁа-яёA-Za-z])(ПК|УК|АК|ДК)\s*,?\s*ст(?:атья|атьи|атье|\.)?\s*№?\s*(\d+(?:\.\d+)*)'
)
_CODEX_TITLE_MARKERS = {
    "ПК": "процессуальн",
    "УК": "уголовн",
    "АК": "административн",
    "ДК": "дорожн",
}


def _numbers_in_text(text: str) -> set:
    return set(m.group(1) for m in _ARTICLE_NUM_RE.finditer(text or ""))


def _ref_num(ref: str) -> Optional[str]:
    """Номер структурной единицы из article_ref — ПЕРВОЕ число заголовка: «Статья 19. Задержание
    на срок 24 часа» → «19» (числа из НАЗВАНИЯ статьи номерами не считаем)."""
    m = re.search(r'\d+(?:\.\d+)*', ref or "")
    return m.group(0) if m else None


def _numbers_in_refs(chunks: List[Dict[str, Any]]) -> set:
    nums = set()
    for c in chunks:
        n = _ref_num(c.get("article_ref") or "")
        if n:
            nums.add(n)
    return nums


def _codex_matches_title(codex: str, title: Optional[str]) -> bool:
    """True, если документ чанка похож на кодекс из сокращения («УК» ↔ «Уголовный кодекс …»)."""
    t = (title or "").lower()
    marker = _CODEX_TITLE_MARKERS.get(codex, "")
    if marker and marker in t:
        return True
    # Фолбэк: само сокращение отдельным словом в названии («ПК Сан-Андреас»); границы слова,
    # чтобы «ук» не ловился внутри «указ».
    return bool(re.search(r'(?<![а-яёa-z])' + codex.lower() + r'(?![а-яёa-z])', t))


def _codexes_by_num(text: str) -> Dict[str, set]:
    """Какие кодексы текст называет рядом с каждым номером статьи: {"17": {"ПК"}, ...}."""
    out: Dict[str, set] = {}
    for m in _CODEX_ART_RE.finditer(text or ""):
        out.setdefault(m.group(2), set()).add(m.group(1))
    return out


def _citation_guard(advice: str, chunks: List[Dict[str, Any]], server_id: str) -> None:
    """Логирует, если модель сослалась на статью вне поданного контекста (возможная выдумка)."""
    outside = _numbers_in_text(advice) - _numbers_in_refs(chunks)
    if outside:
        logger.warning("[ADVICE] citation-guard: ссылки вне контекста server=%s статьи=%s",
                       server_id, sorted(outside))


def _used_sources(answer_text: str, chunks: List[Dict[str, Any]]) -> List[str]:
    """Источники для чипсов оверлея: статьи, РЕАЛЬНО упомянутые в тексте ответа.

    Матчим номера из текста (_ARTICLE_NUM_RE) с ПЕРВЫМ номером article_ref чанка (номер
    структурной единицы, не числа из названия); если рядом с номером в ответе назван кодекс
    («УК ст. 17») — дополнительно сверяем его с title чанка, чтобы «ПК ст. 17» не подсвечивала
    одноимённую статью УК. Клиент рендерит «ст. %1» сам. Пересечение пусто → фолбэк на все
    найденные (как раньше).
    """
    used_nums = _numbers_in_text(answer_text)
    codexes = _codexes_by_num(answer_text)
    seen = set()
    out: List[str] = []
    for c in chunks:
        ref = c.get("article_ref")
        if not ref or ref in seen:
            continue
        num = _ref_num(ref)
        if not num or num not in used_nums:
            continue
        named = codexes.get(num)
        if named and not any(_codex_matches_title(cx, c.get("title")) for cx in named):
            continue  # в ответе статья названа с ДРУГИМ кодексом — этот чанк не использован
        seen.add(ref)
        out.append(ref)
    return out or _sources_from_chunks(chunks)


# --- Enforcement (этап 4): построчная фильтрация пунктов с фантомными статьями/частями/цитатами ---
# Формат v3 «пункт = одна строка» делает проверку детерминированной и дешёвой.
_POINT_LINE_RE = re.compile(r'^\s*[—–\-•]')  # строка-пункт обоснования начинается с тире/буллета
_ART_PART_RE = re.compile(
    r'(?<![а-яёa-z])ст(?:атья|атьи|атье|\.)?\s*№?\s*(\d+(?:\.\d+)*)\s*,?\s*ч(?:асть|асти|\.)?\s*(\d+(?:\.\d+)*)',
    re.IGNORECASE,
)
_QUOTE_RE = re.compile(r'«([^«»]+)»')

# Маркеры частей в тексте статьи: «ч. M» / «часть M» либо построчная нумерация «M.» / «M)»
# в начале строки. Нет НИ ОДНОГО — свод без явной разметки частей, guard (б) пропускаем (fail-open).
_PART_MARKER_RE = re.compile(
    r'(?<![а-яёa-z])(?:ч\s*\.?\s*\d|часть\s+\d)|^\s*\d+(?:\.\d+)*[.)]\s',
    re.IGNORECASE | re.MULTILINE,
)


def _normalize_for_match(s: str) -> str:
    """Нормализация для сравнения «цитата ⊂ контекст»: lower, ё→е, тире/дефисы → «-»
    (модели «типографизируют» пунктуацию: дефис источника становится тире), схлопнутые пробелы."""
    s = (s or '').lower().replace('ё', 'е')
    s = re.sub(r'[—–−-]', '-', s)
    return re.sub(r'\s+', ' ', s).strip()


def _norm_fragment(s: str) -> str:
    """Нормализованный фрагмент цитаты: дополнительно обрезаем пунктуацию по краям."""
    return _normalize_for_match(s).strip(' .,;:!?…—–-«»"\'()')


# Доля значимых слов цитаты, которая должна найтись в тексте нормы, чтобы счесть её
# подтверждённой при НЕточном совпадении (модель часто обрезает/переставляет/слегка
# переформулирует фразу нормы даже при установке «дословно»). Калибровано golden-прогоном:
# точное-подстрочное сравнение ложно резало валидные почти-цитаты (напр. ПК ст. 12
# «...указать источник своей осведомленности» — фраза ЕСТЬ в норме, но обрезана моделью).
_QUOTE_WORD_MIN_LEN = 4        # короткие слова (предлоги/союзы) в расчёт не берём
_QUOTE_SUPPORT_RATIO = 0.7     # ≥70% значимых слов фрагмента — в тексте нормы


def _content_words(norm_content: str) -> set:
    """Множество значимых слов нормализованного контента (для нечёткой сверки цитат)."""
    return {w.strip('.,;:!?()«»"\'-') for w in norm_content.split()
            if len(w) >= _QUOTE_WORD_MIN_LEN}


def _stem_match(a: str, b: str) -> bool:
    """Слова считаем «одним словом в разных формах», если общий префикс ≥4 символов И
    покрывает ≥60% короткого слова. Дешёвая замена лемматизации под русское склонение:
    «горячие»↔«горячим», «следы»↔«следам», «исключительных»↔«исключительности» совпадут,
    а «неотложности» vs «отлагательства» — нет (фабрикация/иной корень)."""
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n >= 4 and n >= 0.6 * min(len(a), len(b))


def _word_supported(w: str, content_words: set) -> bool:
    """Слово цитаты есть в норме точно ИЛИ в иной словоформе (общий корень)."""
    if w in content_words:
        return True
    return any(_stem_match(w, cw) for cw in content_words)


def _fragment_supported(frag_n: str, norm_content: str, content_words: set) -> bool:
    """Подтверждена ли цитата текстом нормы. Быстрый путь — точная подстрока; иначе нечётко:
    ≥70% значимых слов фрагмента есть в норме (точно или в иной словоформе по корню). Ловит
    обрезку/перестановку/склонение/лёгкую переформулировку, но не фабрикацию (у выдуманной
    «цитаты» совпадений по корням мало). Фрагмент из 0–1 значимого слова судим строго."""
    if frag_n in norm_content:
        return True
    words = [w.strip('.,;:!?()«»"\'-') for w in frag_n.split() if len(w) >= _QUOTE_WORD_MIN_LEN]
    if len(words) < 2:
        return False  # слишком мало материала для нечёткой оценки → строгий провал
    hits = sum(1 for w in words if _word_supported(w, content_words))
    return hits / len(words) >= _QUOTE_SUPPORT_RATIO


def _part_in_article_chunks(num: str, part: str, chunks: List[Dict[str, Any]]) -> bool:
    """Мягкий guard пары «ст. N ч. M»: в контенте чанков статьи N встречается «ч. M» / «ч.M» /
    «часть M», дробная запись «N.M» или построчная нумерация «M.» / «M)» в начале строки.
    Лукэхед (?!\\.?\\d) — чтобы часть «4» не подтверждалась записью «ч. 4.1».
    Чанков статьи N нет вовсе — это режет уже проверка (а).
    Fail-open: в чанках статьи ВООБЩЕ нет маркеров частей (экзотическое оформление свода) —
    пропускаем с warning, а не режем валидный пункт."""
    pat = re.compile(
        r'(?<![а-яёa-z])(?:ч\s*\.?\s*|часть\s+)' + re.escape(part) + r'(?!\.?\d)'
        r'|(?<![\d.])' + re.escape(f"{num}.{part}") + r'(?!\.?\d)'
        r'|^\s*' + re.escape(part) + r'[.)](?!\d)',
        re.IGNORECASE | re.MULTILINE,
    )
    article_chunks = [c for c in chunks if _ref_num(c.get("article_ref") or "") == num]
    has_part_markers = False
    for c in article_chunks:
        content = c.get("content") or ""
        if pat.search(content):
            return True
        if not has_part_markers and _PART_MARKER_RE.search(content):
            has_part_markers = True
    if article_chunks and not has_part_markers:
        logger.warning("[ADVICE] enforcement: в чанках ст. %s нет маркеров частей — "
                       "guard «ч. %s» пропущен (fail-open)", num, part)
        return True
    return False


def _scope_chunks_for_line(nums_in_line: set, codexes: Dict[str, set],
                           chunks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Чанки статей, на которые ссылается строка-пункт (с учётом названного кодекса), — скоуп
    проверки цитат (в). Без номеров статей или при пустом сужении (подстраховка) — все чанки."""
    if not nums_in_line:
        return chunks
    scope: List[Dict[str, Any]] = []
    for c in chunks:
        num = _ref_num(c.get("article_ref") or "")
        if num not in nums_in_line:
            continue
        named = codexes.get(num)
        if named and not any(_codex_matches_title(cx, c.get("title")) for cx in named):
            continue
        scope.append(c)
    return scope or chunks


def _line_ok(line: str, chunks: List[Dict[str, Any]]) -> bool:
    """True — строку можно отдать клиенту.

    Строка-ПУНКТ (начинается с «—»/«–»/«-»/«•») проверяется:
      (а)  все номера статей строки есть среди номеров article_ref поданных чанков;
      (а+) кодекс пункта ↔ документ чанка: «УК ст. 17» требует чанк статьи 17 именно УК (title);
      (б)  для пар «ст. N ч. M» — часть M реально встречается в тексте чанков статьи N;
      (в)  каждый «…»-фрагмент в кавычках — подстрока контента чанков СВОИХ статей
           (пункт без номеров статей — глава/«ДК пункт 4.1» — сверяется со всеми чанками).
    Провал любой проверки → DROP (False) + warning.
    НЕ-пункты («Скажи: …», пустые строки) НЕ фильтруем — только warning при фантомных номерах.
    """
    nums_in_line = _numbers_in_text(line)
    if not _POINT_LINE_RE.match(line):
        phantom = nums_in_line - _numbers_in_refs(chunks)
        if phantom:
            logger.warning("[ADVICE] enforcement: фантомные статьи %s в не-пункте (не режем): %r",
                           sorted(phantom), line.strip()[:120])
        return True

    codexes = _codexes_by_num(line)

    if nums_in_line:
        # (а) статьи строки ⊆ статей поданных чанков.
        phantom = nums_in_line - _numbers_in_refs(chunks)
        if phantom:
            logger.warning("[ADVICE] enforcement: DROP пункта — статьи вне контекста %s: %r",
                           sorted(phantom), line.strip()[:120])
            return False

        # (а+) кодекс пункта ↔ документ чанка: «УК ст. 17» не проходит на одной лишь ПК ст. 17.
        for num, named in codexes.items():
            if not any(
                _ref_num(c.get("article_ref") or "") == num
                and any(_codex_matches_title(cx, c.get("title")) for cx in named)
                for c in chunks
            ):
                logger.warning("[ADVICE] enforcement: DROP пункта — «%s ст. %s» не из этого кодекса: %r",
                               "/".join(sorted(named)), num, line.strip()[:120])
                return False

        # (б) «ст. N ч. M» — часть подтверждена текстом чанков статьи N.
        for m in _ART_PART_RE.finditer(line):
            num, part = m.group(1), m.group(2)
            if not _part_in_article_chunks(num, part, chunks):
                logger.warning("[ADVICE] enforcement: DROP пункта — «ст. %s ч. %s» нет в чанках статьи: %r",
                               num, part, line.strip()[:120])
                return False

    # (в) кавычки — дословный (нормализованный) текст из чанков СВОИХ статей пункта
    # (сужение, чтобы цитата из ПК ст. 17 не «подтверждала» пункт про УК ст. 17);
    # проверяем у ЛЮБОГО пункта с кавычками, даже без номеров статей.
    quotes = _QUOTE_RE.findall(line)
    if quotes:
        scope = _scope_chunks_for_line(nums_in_line, codexes, chunks)
        norm_content = _normalize_for_match("\n".join(c.get("content") or "" for c in scope))
        content_words = _content_words(norm_content)
        for quoted in quotes:
            for frag in re.split(r'…|\.\.\.', quoted):
                frag_n = _norm_fragment(frag)
                if len(frag_n) < 3:
                    continue  # пустяковые обрывки после «…»-разбиения не проверяем
                if not _fragment_supported(frag_n, norm_content, content_words):
                    logger.warning("[ADVICE] enforcement: DROP пункта — цитата не из контекста %r: %r",
                                   frag_n[:80], line.strip()[:120])
                    return False
    return True


def _count_points(text: str) -> int:
    """Число строк-пунктов («— …») в ответе — метрика «какой из двух ответов лучше выжил»."""
    return sum(1 for ln in (text or "").split("\n") if _POINT_LINE_RE.match(ln))


def _filter_advice_lines(text: str, chunks: List[Dict[str, Any]]) -> Tuple[str, List[str]]:
    """Прогоняет готовый (non-stream) ответ через построчный enforcement.

    Возвращает (текст без выпавших строк, список выпавших строк-пунктов).
    """
    kept: List[str] = []
    dropped: List[str] = []
    for line in (text or "").split("\n"):
        if _line_ok(line, chunks):
            kept.append(line)
        else:
            dropped.append(line.strip())
    return "\n".join(kept), dropped


def _log_confidence(chunks: List[Dict[str, Any]], server_id: str) -> float:
    max_sim = max((float(c.get("similarity") or 0) for c in chunks), default=0.0)
    if max_sim < getattr(config, "OVERLAY_MIN_SIMILARITY", 0.45):
        logger.info("[ADVICE] низкая уверенность ретривала server=%s max_sim=%.3f", server_id, max_sim)
    return max_sim


ADVICE_SYSTEM_PROMPT = """Ты — голосовой юридический ассистент игрока на RP-сервере (штат Сан-Андреас, GTA5RP).
Игрок ПРЯМО СЕЙЧАС в живой ситуации (задержание, обыск, проверка документов, конфликт с сотрудником, суд) и видит твой ответ на игровом оверлее: пара секунд на чтение, один шанс сказать правильную фразу.

ТЕБЕ ПОДАЮТ: контекст разговора (может отсутствовать), ситуацию (шумный транскрипт живой речи или текстовый вопрос) и блок «Релевантные нормы сервера» — фрагменты законов под заголовками [<заголовок нормы> — <название документа>]; заголовком может быть статья, глава, пункт или только название документа. Нормы нашёл автоматический поиск: среди них есть и решающие дело, и случайные соседи.

ЕДИНСТВЕННЫЙ ИСТОЧНИК ПРАВА — блок «Релевантные нормы сервера». Статьи, прозвучавшие в сцене или истории (например, названные сотрудником), источником НЕ являются: упоминай их только как слова собеседника и сверяй с поданными нормами; нет подтверждения — так и скажи.

ПРОТОКОЛ (выполни молча, в ответ не включай):
ШАГ 1. СЦЕНА. Что происходит, стадия процедуры, СТАТУС каждого участника (гражданский / сотрудник при исполнении / госслужащий) — от статуса зависит квалификация; кому игрок сейчас отвечает (адресат реплики). Если статус не ясен, а от него зависит вывод — не назначай его сам: дай совет по общему случаю и предложи уточнить. Текущая реплика ПРОДОЛЖАЕТ контекст разговора: короткий вопрос («а если он госслужащий?», «а сроки?») относится к предмету прошлых реплик — отвечай на сводный смысл.
ШАГ 2. НОРМЫ. Возьми из поданных норм ТОЛЬКО решающие эту ситуацию, посторонние молча отбрось — норма не становится применимой от того, что её подали. Отобранные читай ЦЕЛИКОМ: части (ч.), пункты, Примечания, Исключения — решающие условия и сроки почти всегда там, а не в первой строке статьи. Определи, какая ИМЕННО часть решает дело. Если две нормы дают несовместимые ответы на один и тот же вопрос — обопрись на ту, чей текст точнее описывает ситуацию (процедура и санкция в разных кодексах — НЕ противоречие).
ШАГ 3. ПОЗИЦИЯ. Что укрепляет позицию игрока В ЕГО РОЛИ и чем её может атаковать оппонент (исключения, дефекты процедуры, сроки, недопустимость доказательств). Реплику строй на самом сильном основании СВОЕЙ позиции; что считать «в свою пользу», определяет роль: для стороны обвинения чужое процедурное нарушение — повод зафиксировать и устранить его, а не повод разрушать собственное дело.

ФОРМАТ ОТВЕТА (строго; по-русски; без преамбул, заголовков и пересказа диалога):
Скажи: «…» — ПЕРВОЙ строкой (единственное исключение — нечитаемый вход, см. ниже): готовая реплика, которую игрок сразу произносит адресату из ШАГА 1 (офицеру, задержанному, судье). Если вход — прямой вопрос игрока тебе («а сроки?», текстовый вопрос) — реплика = готовый ответ по существу. Номер нормы в реплике — только прошедший ПРОВЕРКУ ниже; не прошёл — реплика без номера.
Затем 1–4 пункта обоснования (обычно 2–3; если в задании явно не задан иной объём), каждый — ОДНА строка:
— <Кодекс> ст. <N>[ ч. <M>]: «<фрагмент нормы>» → <вывод для игрока, до 8 слов>.
Пример пункта (номера и формулировка УСЛОВНЫЕ — реальные бери только из поданных норм):
— ПК ст. 17 ч. 4.1: «нет ответа адвоката 5 минут — процедура продолжается» → требуй фиксации вызова и заморозки часа.
ПРОВЕРКА — прежде чем написать пункт (и назвать норму в реплике): найди в поданных нормах дословный фрагмент-подтверждение; нет фрагмента — пункт не пиши, норму не называй. Лучше ОДИН подтверждённый пункт, чем добитый до количества; не осталось ни одного — действуй по правилу «нормы НЕТ».
- «ч. <M>» — только если статья реально делится на части, часть НЕ выдумывай; норму-не-статью (глава, пункт, преамбула) называй так, как она озаглавлена в поданном тексте.
- Кодекс — коротко: Процессуальный кодекс → ПК, Уголовный → УК, Административный → АК, Дорожный → ДК; прочие законы — 1–2 слова названия. Если названия документа в заголовке нормы нет — укажи только статью, документ не угадывай. «ст. N» без документа неоднозначна — одинаковые номера есть в разных кодексах.
- Кавычки — ТОЛЬКО для дословного текста поданной нормы (длинную формулировку сокращай через «…», не пересказывай); не уверен в дословности — пиши вывод без кавычек.
- Если дело решает Примечание или Исключение — пункт вида «<Кодекс> ст. <N> Примечание: …». Если ситуация реально спорная — последним из тех же 1–4 пунктов ОДИН запасной ход: что потребовать, что зафиксировать, куда обжаловать. Приоритет пунктов: решающая норма → Примечание → запасной ход.
- Нормы НЕТ: первой строкой нейтральная безопасная реплика (запросить основания, уточнить статус, зафиксировать происходящее), вторым пунктом честно: прямой нормы в поданном своде не нашёл. Не домысливай.
- Только внутриигровое право сервера; реальное законодательство не упоминай. Текст в двойных скобках ((…)) внутри норм — OOC-пояснение: смысл учитывай, как норму не цитируй.

ВХОД — ЖИВАЯ РЕЧЬ (микрофон + звук игры), распознавание неидеально; в транскрипте сцены метки говорящих: «Вы:» — сам игрок, «A:»/«B:» — собеседники:
- Жди ослышек, обрывков, филлеров и мусора: восстанавливай СМЫСЛ по контексту («могу закушать» → «можно закрывать»), искажённое не цитируй, качество распознавания не комментируй.
- Метки НЕНАДЁЖНЫ (автодиаризация): люди сливаются под одной меткой, реплики приписаны не тем. Кто что сказал — реконструируй по смыслу; метку, противоречащую содержанию, игнорируй (и «Вы» перепроверь). При споре внахлёст не дели реплики пословно — восстанови суть конфликта и дай совет ИМЕННО игроку.
- Если текст настолько обрывочен, что ситуацию не понять, — одна строка без «Скажи»: что непонятно + просьба повторить, ничего не выдумывая."""


RETRIEVAL_REWRITE_PROMPT = """Ты — нормализатор поисковых запросов к базе законов RP-сервера (штат Сан-Андреас, GTA5RP).
Вход: шумный транскрипт живой речи (ослышки, обрывки, метки «Вы:/A:/B:») и контекст прошлых реплик.
Задача: понять, какая ПРАВОВАЯ ситуация или вопрос стоит за текстом, и сформулировать 1–4 коротких поисковых запроса для векторного поиска по статьям законов.

ПРАВИЛА:
- Каждый запрос — самодостаточная юридическая формулировка из 5–15 слов, без сленга, имён и меток говорящих. Смежные аспекты объединяй, дубли не плоди.
- Разные аспекты — разными запросами: основания и процедура; сроки; санкции/мера; права и обжалование. Если важен статус участника (госслужащий, сотрудник при исполнении) — он должен попасть в запрос.
- Текущая реплика продолжает контекст: «а если он госслужащий?» → внеси предмет из контекста: «задержание государственного служащего: порядок и уведомление».
- Если во входе или контексте назван НОМЕР статьи или кодекс («а что по ст. 19?») — включи их в запрос дословно вместе с темой: «статья 19 процессуального кодекса — основания задержания». Если в прошлых репликах по предмету вопроса уже называлась конкретная статья — сформируй отдельный запрос по её теме, чтобы она снова попала в выдачу.
- Переводи разговорное в термины законов: «ДМ» → умышленное убийство без причины; «ДБ» → нападение с использованием транспортного средства; «РК/ТК/СК» → умышленное убийство; «миранда» → права задержанного при задержании; «закрыть/посадить» → арест, заключение под стражу; «обыскали тачку» → обыск транспортного средства; «ксива» → удостоверение; «госник» → государственный служащий.
- OOC-термины правил сервера и админ-наказаний (деморган, варн, КД, ПГ, НРП, FearRP, МГ) в законах отсутствуют — в запросы их не выноси; если за ними стоит IC-деяние (угрозы, принуждение, нападение) — сформулируй запрос по нему.
- НЕ отвечай на вопрос по существу и не выдумывай фактов, которых нет во входе.
- Пустой список {"queries": []} верни ТОЛЬКО если правового смысла нет совсем (тишина, мусор распознавания, чистый оффтоп). Сомневаешься или формулировка странная — всё равно сформулируй лучший возможный запрос: пусть решает поиск.
- Верни СТРОГО JSON: {"queries": ["...", "..."]} без любого другого текста."""


# Метки спикеров из локальной диаризации оверлея: "Вы:" (сам игрок, микрофон) или "A:/B:/C2:" в начале строки.
_SPEAKER_LINE_RE = re.compile(r'^\s*(Вы|[A-Z]\d?)\s*:', re.MULTILINE)


def _looks_multispeaker(text: str) -> bool:
    """True, если текст — транскрипт сцены с ≥2 разными метками говорящих (Вы:/A:/B:/C:)."""
    labels = set(m.group(1) for m in _SPEAKER_LINE_RE.finditer(text or ""))
    return len(labels) >= 2


def _build_user_prompt(situation: str, history: Optional[str], context: str) -> str:
    """Сборка user-промпта. Если ситуация — транскрипт сцены (диаризация), маркируем её иначе
    и явно просим распознать роли говорящих и адресовать совет игроку."""
    parts: List[str] = []
    if history:
        parts.append(f"Контекст разговора (последние реплики):\n{history}\n")
    if _looks_multispeaker(situation):
        parts.append(f"Транскрипт сцены (диаризация, несколько говорящих):\n{situation}\n")
        parts.append(f"Релевантные нормы сервера:\n{context}\n")
        parts.append("Определи роли говорящих по содержанию и дай короткий совет ИГРОКУ по правилам выше.")
    else:
        parts.append(f"Ситуация/вопрос:\n{situation}\n")
        parts.append(f"Релевантные нормы сервера:\n{context}\n")
        parts.append("Дай короткий совет по правилам выше.")
    return "\n".join(parts)


def _pick_fast_model(model_tier: Optional[str]) -> str:
    """A/B быстрой модели совета: 'lite' → flash-lite, иначе flash (дефолт). Белый список —
    произвольную модель от клиента НЕ принимаем."""
    if model_tier == "lite":
        return config.OVERLAY_ADVICE_MODEL_LITE
    return config.OVERLAY_ADVICE_MODEL_FAST


async def rewrite_queries(situation: str, history: Optional[str]) -> List[str]:
    """Нормализатор запроса (5.2): шумный транскрипт + история → 1–4 юридических поисковых запроса.

    flash-lite (config.OVERLAY_REWRITE_MODEL), строгий JSON, max_retries=1, общий таймаут 3с.
    Fail-open: ЛЮБАЯ ошибка/таймаут/пусто → [] — совет НЕ должен зависеть от риврайтера
    (вызывающий при [] ретривалит по сырой ситуации, как раньше).
    """
    situation = (situation or "").strip()
    if not situation:
        return []

    parts: List[str] = []
    if history:
        parts.append(f"Контекст разговора (последние реплики):\n{history}\n")
    parts.append(f"Вход (ситуация/вопрос):\n{situation}")
    user_prompt = "\n".join(parts)

    from google.genai import types

    schema = {
        "type": "object",
        "properties": {
            "queries": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 4,
            },
        },
        "required": ["queries"],
    }

    try:
        model = config.OVERLAY_REWRITE_MODEL

        async def _call():
            client = get_current_client()
            if not client:
                raise RuntimeError("genai_client не инициализирован")
            return await client.aio.models.generate_content(
                model=model,
                contents=user_prompt,
                config=types.GenerateContentConfig(
                    system_instruction=RETRIEVAL_REWRITE_PROMPT,
                    response_mime_type="application/json",
                    response_schema=schema,
                ),
            )

        resp = await asyncio.wait_for(
            execute_vertex_request(
                func=_call, model_name=model,
                priority=RequestPriority.HIGH, max_retries=1,
            ),
            timeout=3.0,
        )
        raw = (getattr(resp, "text", "") or "").strip()
        parsed = json.loads(raw) if raw else {}
        queries = [q.strip() for q in (parsed.get("queries") or []) if isinstance(q, str) and q.strip()]
        return queries[:4]
    except Exception as e:
        logger.warning("[ADVICE] риврайтер запросов недоступен (fail-open → сырой запрос): %s", e)
        return []


def _register_stream_call(model: str) -> None:
    """Регистрирует stream-вызов в QPM-окне лимитера.

    generate_content_stream нельзя завернуть в execute_vertex_request (это async-генератор,
    полную обвязку не натянуть) — но расход квоты фиксируем, чтобы параллельные не-stream
    вызовы видели реальный QPM и не переливали квоту.
    """
    try:
        base = vertex_rate_limiter._get_model_base_name(model)
        vertex_rate_limiter.request_history[base].append(time.time())
    except Exception:
        logger.debug("[ADVICE] не удалось зарегистрировать stream-вызов в QPM-окне", exc_info=True)


async def generate_advice(
    server_id: str,
    situation: str,
    *,
    history: Optional[str] = None,
    deep: bool = False,
    top_k: Optional[int] = None,
    mode: Optional[str] = None,
    model_tier: Optional[str] = None,
    project_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Возвращает {advice, sources, chunks}. deep=True -> Pro-модель (глубокий разбор)."""
    situation = (situation or "").strip()
    if not situation:
        return {"advice": "", "sources": [], "chunks": []}

    # Режим «Обзвон»: ретрив из СВОДА ПРАВИЛ (не из игровой законки). Слои свода зависят от игрового
    # проекта/сервера: Majestic — единый свод; GTA5RP — общие правила проекта + правила сервера (union).
    retrieval_targets = server_id
    if normalize_mode(mode) == "leadership":
        retrieval_targets = leadership_layers(project_id, server_id)
        logger.info("[ADVICE] Обзвон: project=%s server=%s → слои свода %s",
                    project_id, server_id, retrieval_targets)

    top_k = top_k or config.OVERLAY_RETRIEVAL_TOP_K
    # Риврайтер (5.2): шумный транскрипт → 1–4 юридических запроса, мульти-ретривал с union.
    # Fail-open: пустой риврайт → старый путь по сырой ситуации.
    t0 = time.monotonic()
    queries = await rewrite_queries(situation, history)
    rewrite_ms = int((time.monotonic() - t0) * 1000)
    t0 = time.monotonic()
    if queries:
        logger.info("[ADVICE] риврайт запросов server=%s: %s", server_id, queries)
        chunks = await retrieve_multi(retrieval_targets, queries, mode=mode)
        if not chunks:
            # Fail-open глубже риврайтера: запросы есть, но мульти-ретривал пуст (пустой
            # батч-эмбеддинг / ни одного семени) — пробуем сырой путь, как при пустом риврайте.
            logger.warning("[ADVICE] мульти-ретривал пуст server=%s — фолбэк на сырой retrieve", server_id)
            chunks = await retrieve(retrieval_targets, situation, top_k=top_k, mode=mode)
    else:
        chunks = await retrieve(retrieval_targets, situation, top_k=top_k, mode=mode)
    retrieve_ms = int((time.monotonic() - t0) * 1000)
    if not chunks and normalize_mode(mode) != "leadership":
        return {
            "advice": "По выбранному серверу не нашёл релевантных норм. "
                      "Уточни ситуацию или проверь, что свод сервера проиндексирован.",
            "sources": [], "chunks": [],
        }
    # leadership с пустым ретривом (вопрос по ИРЛ-праву / правило не совпало) — отвечаем из знаний модели.

    context = format_chunks_for_prompt(chunks)
    model = config.OVERLAY_ADVICE_MODEL_DEEP if deep else _pick_fast_model(model_tier)

    user_prompt = _build_user_prompt(situation, history, context)

    from google.genai import types

    def _make_call(prompt: str):
        async def _call():
            client = get_current_client()
            if not client:
                raise RuntimeError("genai_client не инициализирован")
            return await client.aio.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(system_instruction=get_role_prompt(mode, ADVICE_SYSTEM_PROMPT)),
            )
        return _call

    t0 = time.monotonic()
    resp = await execute_vertex_request(
        func=_make_call(user_prompt), model_name=model,
        priority=RequestPriority.HIGH, max_retries=2,
    )
    advice = (getattr(resp, "text", "") or "").strip()
    # Тайминги этапов — телеметрия для калибровки таймаута риврайтера и top_k по проду.
    logger.info("[ADVICE] тайминги server=%s: rewrite=%dмс retrieve=%dмс generate=%dмс",
                server_id, rewrite_ms, retrieve_ms, int((time.monotonic() - t0) * 1000))

    _log_confidence(chunks, server_id)
    _citation_guard(advice, chunks, server_id)  # диагностический лог (оставлен)

    # Enforcement (этап 4): режем строки-пункты с фантомными статьями/частями/цитатами.
    advice, dropped = _filter_advice_lines(advice, chunks)
    if dropped:
        # ОДИН повторный вызов с указанием ошибки; повторный ответ тоже фильтруется (второго ретрая нет).
        logger.warning("[ADVICE] enforcement: вырезано %d строк server=%s — повторный вызов",
                       len(dropped), server_id)
        retry_prompt = (
            user_prompt
            + "\n\nТы сослался на нормы, которых нет в предоставленных фрагментах: "
            + "; ".join(d for d in dropped if d)
            + ". Перепиши ответ строго по поданным нормам."
        )
        try:
            resp2 = await execute_vertex_request(
                func=_make_call(retry_prompt), model_name=model,
                priority=RequestPriority.HIGH, max_retries=1,
            )
            advice2 = (getattr(resp2, "text", "") or "").strip()
            if advice2:
                _citation_guard(advice2, chunks, server_id)
                advice2, dropped2 = _filter_advice_lines(advice2, chunks)
                if dropped2:
                    logger.warning("[ADVICE] enforcement: повторный ответ тоже с фантомами "
                                   "(вырезано %d строк) server=%s", len(dropped2), server_id)
                # Повтор мог выйти ХУЖЕ первого — отдаём тот, где после фильтра выжило больше пунктов.
                if _count_points(advice2) >= _count_points(advice):
                    advice = advice2
                else:
                    logger.info("[ADVICE] enforcement: повторный ответ слабее первого "
                                "(%d < %d пунктов) — оставляем первый",
                                _count_points(advice2), _count_points(advice))
        except Exception as e:
            logger.warning("[ADVICE] enforcement: повторный вызов не удался (%s) — "
                           "отдаём отфильтрованный первый ответ", e)

    # Источники для подсветки на оверлее — только статьи, реально использованные в ответе
    # (фолбэк при пустом пересечении — все найденные, как раньше).
    return {"advice": advice, "sources": _used_sources(advice, chunks), "chunks": chunks}


def _sources_from_chunks(chunks: List[Dict[str, Any]]) -> List[str]:
    seen = set()
    out: List[str] = []
    for c in chunks:
        ref = c.get("article_ref")
        if ref and ref not in seen:
            seen.add(ref)
            out.append(ref)
    return out


async def stream_advice(
    server_id: str,
    situation: str,
    *,
    history: Optional[str] = None,
    deep: bool = False,
    top_k: Optional[int] = None,
    mode: Optional[str] = None,
    model_tier: Optional[str] = None,
    project_id: Optional[str] = None,
):
    """Стриминговая версия: async-генератор событий (type, data).

    События:
      ("sources", {"sources": [...], "chunks_used": N})                  — один раз в начале (все найденные),
      ("delta",   {"text": "..."})                                       — куски ответа (только завершённые строки),
      ("sources", {"sources": [...], "chunks_used": N, "refined": True}) — уточнённые источники после дельт,
      ("done",    {})                                                     — конец,
      ("error",   {"message": "..."})                                     — при ошибке.
    """
    situation = (situation or "").strip()
    if not situation:
        yield ("error", {"message": "Пустая ситуация"})
        return

    # Режим «Обзвон»: ретрив из СВОДА ПРАВИЛ (не из игровой законки). Слои свода зависят от игрового
    # проекта/сервера: Majestic — единый свод; GTA5RP — общие правила проекта + правила сервера (union).
    retrieval_targets = server_id
    if normalize_mode(mode) == "leadership":
        retrieval_targets = leadership_layers(project_id, server_id)
        logger.info("[ADVICE] Обзвон(stream): project=%s server=%s → слои свода %s",
                    project_id, server_id, retrieval_targets)

    top_k = top_k or config.OVERLAY_RETRIEVAL_TOP_K
    # Риврайтер (5.2): fail-open — пустой риврайт → старый путь по сырой ситуации.
    t0 = time.monotonic()
    queries = await rewrite_queries(situation, history)
    rewrite_ms = int((time.monotonic() - t0) * 1000)
    try:
        t0 = time.monotonic()
        if queries:
            logger.info("[ADVICE] риврайт запросов server=%s: %s", server_id, queries)
            chunks = await retrieve_multi(retrieval_targets, queries, mode=mode)
            if not chunks:
                # Fail-open глубже риврайтера: мульти-ретривал пуст → сырой путь (как при пустом риврайте).
                logger.warning("[ADVICE] мульти-ретривал пуст server=%s — фолбэк на сырой retrieve", server_id)
                chunks = await retrieve(retrieval_targets, situation, top_k=top_k, mode=mode)
        else:
            chunks = await retrieve(retrieval_targets, situation, top_k=top_k, mode=mode)
        retrieve_ms = int((time.monotonic() - t0) * 1000)
    except Exception as e:
        logger.exception("[ADVICE] retrieve failed")
        yield ("error", {"message": f"Ошибка поиска норм: {e}"})
        return

    yield ("sources", {"sources": _sources_from_chunks(chunks), "chunks_used": len(chunks)})

    if not chunks and normalize_mode(mode) != "leadership":
        yield ("delta", {"text": "По выбранному серверу не нашёл релевантных норм."})
        yield ("done", {})
        return
    # leadership с пустым ретривом (ИРЛ-право / правило не совпало) — отвечаем из знаний модели.

    context = format_chunks_for_prompt(chunks)
    model = config.OVERLAY_ADVICE_MODEL_DEEP if deep else _pick_fast_model(model_tier)
    user_prompt = _build_user_prompt(situation, history, context)

    from google.genai import types

    try:
        client = get_current_client()
        if not client:
            raise RuntimeError("genai_client не инициализирован")
        _log_confidence(chunks, server_id)
        # Stream идёт мимо execute_vertex_request — хотя бы регистрируем вызов в QPM-окне лимитера.
        _register_stream_call(model)
        t_gen = time.monotonic()
        stream = await client.aio.models.generate_content_stream(
            model=model,
            contents=user_prompt,
            config=types.GenerateContentConfig(system_instruction=get_role_prompt(mode, ADVICE_SYSTEM_PROMPT)),
        )

        parts_out: List[str] = []   # сырой ответ модели целиком (для citation-guard-лога)
        sent_parts: List[str] = []  # что реально ушло клиенту (для уточнённых sources)
        dropped: List[str] = []     # вырезанные enforcement'ом строки-пункты
        buf = ""
        ttfb_ms = -1                # время до ПЕРВОГО куска от модели (-1 = модель ничего не отдала)

        async for chunk in stream:
            text = getattr(chunk, "text", "") or ""
            if not text:
                continue
            if ttfb_ms < 0:
                ttfb_ms = int((time.monotonic() - t_gen) * 1000)
            parts_out.append(text)
            buf += text
            # Enforcement (этап 4): отдаём ТОЛЬКО завершённые строки — проверка строки целиком.
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                if _line_ok(line, chunks):
                    sent_parts.append(line + "\n")
                    yield ("delta", {"text": line + "\n"})
                else:
                    dropped.append(line.strip())
        # Финальный flush остатка без \n.
        if buf:
            if _line_ok(buf, chunks):
                sent_parts.append(buf)
                yield ("delta", {"text": buf})
            else:
                dropped.append(buf.strip())

        if dropped:
            # Реплика «Скажи:» уже ушла клиенту — повторного вызова в stream нет, только лог
            # (в т.ч. если выпали ВСЕ пункты).
            logger.warning("[ADVICE] enforcement(stream): вырезано %d строк server=%s",
                           len(dropped), server_id)
        _citation_guard("".join(parts_out), chunks, server_id)  # диагностический лог по сырому ответу
        # Тайминги этапов — телеметрия для калибровки таймаута риврайтера (3с) и top_k по проду.
        logger.info("[ADVICE] stream тайминги server=%s: rewrite=%dмс retrieve=%dмс ttfb=%dмс",
                    server_id, rewrite_ms, retrieve_ms, ttfb_ms)

        # Уточнённые источники: только статьи, реально использованные в отправленном тексте.
        yield ("sources", {
            "sources": _used_sources("".join(sent_parts), chunks),
            "chunks_used": len(chunks),
            "refined": True,
        })
        yield ("done", {})
    except Exception as e:
        err = str(e)
        if "429" in err or "RESOURCE_EXHAUSTED" in err or type(e).__name__ == "ResourceExhausted":
            logger.warning("[ADVICE] stream: квота AI исчерпана (429): %s", e)
            yield ("error", {"message": "AI-сервис временно перегружен — попробуй через минуту."})
        else:
            logger.exception("[ADVICE] stream generation failed")
            yield ("error", {"message": f"Ошибка генерации: {e}"})


# END OF FILE services/advice_service.py
