# START OF FILE services/law_search_service.py

"""
Умный поиск по законке для оверлея: гибрид БЕЗ LLM в рантайме.

Пайплайн запроса («какая статья за халатность» / «статья двенадцать точка один»):
  normalize_query: срез вопросительной обёртки, числительные → цифры, кандидаты кода
  → независимые ноги параллельно: code/title, строгие phrase/legal/intent-варианты,
    synonym exact/fuzzy, FTS russian, trigram и локальная FRIDA по canonical
    law_article_embeddings. У каждой ноги свой outcome и timeout;
    если FRIDA недоступна, поиск продолжает работать по keyword-ногам.
  → слияние Reciprocal Rank Fusion с весами ног → карточки + «ещё релевантное»
    (law_article_related, предрассчитано офлайн).

В рантайме нет LLM-вызовов: только локальный CPU-инференс FRIDA и PostgreSQL.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import weakref
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Awaitable, Dict, List, Literal, Mapping, Optional, Tuple

import config
from database.manager import db_manager
from services.law_search_ranking import (
    build_evidence_query,
    evaluate_applicability,
    lexical_dominance_order,
)
from services.law_title_utils import recover_rating_metadata_title

logger = logging.getLogger(__name__)

# Explicitly stored with every quality event.  Bump this only when query
# interpretation, leg scoring, fusion or result prioritisation changes.
LAW_SEARCH_ALGORITHM_VERSION = "hybrid-rrf-2026-07-29-source-evidence-numeric-intent-title-v2"

# --- Нормализация запроса ----------------------------------------------------------------------

# Обёрточные слова голосовых запросов: срезаются ЦЕЛИКОМ по всему запросу —
# смысловое ядро для FTS/триграмм/словаря остаётся («какая статья за халатность» → «халатность»).
_WRAPPER_WORDS = {
    'какая', 'какой', 'какие', 'каком', 'что', 'чего', 'будет', 'грозит', 'светит',
    'сколько', 'дают', 'дадут', 'полагается', 'статья', 'статью', 'статьи', 'ст',
    'за', 'по', 'про', 'если', 'мне', 'меня', 'нам', 'это', 'эта', 'этот',
    'подскажи', 'скажи', 'найди', 'покажи', 'поищи', 'ищи', 'нужна', 'нужно', 'надо',
    'пожалуйста', 'а', 'ну', 'вот', 'там', 'наказание', 'наказания', 'бывает',
    # филлеры-паразиты (шумят в FTS relaxed-OR: «простой»/«мирный собраниях» и т.п.)
    'просто', 'так', 'типа', 'короче', 'короч', 'вообще', 'прям', 'блин', 'жи', 'же', 'бы',
}

# Универсальный RP/криминальный сленг → канонический юридический концепт. ОБЩАЯ карта в КОДЕ
# (одна на все server_id, деплой = везде) — в отличие от per-server law_synonyms (шумного авто-словаря).
# Префиксный матч гасит склонения («госнику»/«госников» → «госник»). Только ОДНОЗНАЧНЫЙ сленг:
# короткие/двусмысленные («коп»/«план») НЕ включаем. ПОЛНАЯ карта (122 пары; воркфлоу
# Harvest→Generate→Vet→Synthesize): вет доказал — 0 стемов-префиксов друг друга (любой токен =
# максимум ОДИН стем, порядок нейтрален), 0 стемов с «ё» (запрос ё→е нормализуется ДО маппинга).
# Многословный canonical («холодное оружие») разбивается на токены в normalize_query (ниже).
_SLANG_STEMS = (
    ('росгвард', 'сотрудник'),
    ('гаишник', 'сотрудник'),
    ('следак', 'сотрудник'),
    ('госник', 'сотрудник'),
    ('гостик', 'сотрудник'),
    ('легав', 'сотрудник'),
    ('госик', 'сотрудник'),
    ('фибо', 'сотрудник'),
    ('лспд', 'сотрудник'),
    ('мент', 'сотрудник'),
    ('дпс', 'сотрудник'),
    ('терпил', 'потерпевший'),
    ('чинуш', 'должностное лицо'),
    ('губер', 'губернатор'),
    ('дробаш', 'оружие'),
    ('пистол', 'оружие'),
    ('калаш', 'оружие'),
    ('ствол', 'оружие'),
    ('волын', 'оружие'),
    ('пушк', 'оружие'),
    ('помп', 'оружие'),
    ('глок', 'оружие'),
    ('мачете', 'холодное оружие'),
    ('заточк', 'холодное оружие'),
    ('тесак', 'холодное оружие'),
    ('перо', 'холодное оружие'),
    ('шило', 'холодное оружие'),
    ('финк', 'холодное оружие'),
    ('электрошок', 'спецсредство'),
    ('дубинк', 'спецсредство'),
    ('тайзер', 'спецсредство'),
    ('тэйзер', 'спецсредство'),
    ('перцов', 'спецсредство'),
    ('шокер', 'спецсредство'),
    ('закладк', 'наркотики'),
    ('экстази', 'наркотики'),
    ('торчок', 'наркотики'),
    ('дозняк', 'наркотики'),
    ('наркот', 'наркотики'),
    ('герыч', 'наркотики'),
    ('спайс', 'наркотики'),
    ('травк', 'наркотики'),
    ('нарик', 'наркотики'),
    ('гандж', 'наркотики'),
    ('шишк', 'наркотики'),
    ('кокс', 'наркотики'),
    ('меф', 'наркотики'),
    ('амф', 'наркотики'),
    ('барыг', 'сбыт'),
    ('упорот', 'наркотическое опьянение'),
    ('обкур', 'наркотическое опьянение'),
    ('укур', 'наркотическое опьянение'),
    ('кайф', 'наркотическое опьянение'),
    ('подвыпивш', 'опьянение'),
    ('навеселе', 'опьянение'),
    ('подшофе', 'опьянение'),
    ('нетрезв', 'опьянение'),
    ('выпивш', 'опьянение'),
    ('обдолб', 'опьянение'),
    ('поддат', 'опьянение'),
    ('квашен', 'опьянение'),
    ('накур', 'опьянение'),
    ('бухой', 'опьянение'),
    ('алкаш', 'опьянение'),
    ('датый', 'опьянение'),
    ('пьян', 'опьянение'),
    ('нажр', 'опьянение'),
    ('прикончи', 'убийство'),
    ('укокош', 'убийство'),
    ('грохну', 'убийство'),
    ('замочи', 'убийство'),
    ('зареза', 'убийство'),
    ('пореши', 'убийство'),
    ('мокрух', 'убийство'),
    ('мокруш', 'убийство'),
    ('навалял', 'телесные повреждения'),
    ('отметел', 'телесные повреждения'),
    ('отдубас', 'телесные повреждения'),
    ('отпина', 'телесные повреждения'),
    ('мутузи', 'телесные повреждения'),
    ('избива', 'телесные повреждения'),
    ('избие', 'телесные повреждения'),
    ('избил', 'телесные повреждения'),
    ('избит', 'телесные повреждения'),
    ('пырн', 'телесные повреждения'),
    ('гопстоп', 'разбой'),
    ('гопник', 'разбой'),
    ('грабану', 'грабеж'),
    ('карманник', 'кража'),
    ('домушник', 'кража'),
    ('спизд', 'кража'),
    ('стыр', 'кража'),
    ('стащ', 'кража'),
    ('казнокрад', 'хищение'),
    ('прикарман', 'хищение'),
    ('распил', 'хищение'),
    ('лохотрон', 'мошенничество'),
    ('развод', 'мошенничество'),
    ('скаме', 'мошенничество'),
    ('кидал', 'мошенничество'),
    ('фрод', 'мошенничество'),
    ('крышев', 'вымогательство'),
    ('поборы', 'вымогательство'),
    ('отжал', 'вымогательство'),
    ('рэкет', 'вымогательство'),
    ('рекет', 'вымогательство'),
    ('подмаз', 'взятка'),
    ('откат', 'взятка'),
    ('бандит', 'вооруженное формирование'),
    ('угон', 'угон'),
    ('угна', 'угон'),
    ('встречк', 'встречная полоса'),
    ('бесправ', 'вождение без прав'),
    ('безправ', 'вождение без прав'),
    ('лишенк', 'вождение без прав'),
    ('стритрейс', 'превышение скорости'),
    ('подрез', 'опасное вождение'),
    ('лихач', 'опасное вождение'),
    ('дрифт', 'опасное вождение'),
    ('мигалк', 'спецтранспорт'),
    ('обоч', 'обочина'),
)

# Разговорная формулировка -> общеупотребимый юридический эквивалент. В отличие от
# law_synonyms это не привязка к статье/серверу: вариант запроса прогоняется по каталогу
# КАЖДОГО server_id. Исходный вариант всегда сохраняется, поэтому серверы, где статья
# прямо называется «Угон ...», не проигрывают серверам с названием
# «Неправомерное завладение ...».
_LEGAL_EQUIVALENT_STEMS = (
    ('угон', ('неправомерное', 'завладение')),
)

# Ситуационный запрос «человек находится в розыске» на Majestic означает
# самостоятельный состав об уклонении от наказания/нахождении в активном розыске.
# Формулировки различаются между кодексами, поэтому ищем их ПО СМЫСЛУ в текущем
# server_id, а не пришиваем номер статьи (16.15 на другом сервере может быть доносом).
# Порядок важен: сначала общий состав, который владелец продукта ожидает видеть
# первым, затем его реальные редакционные аналоги.
_WANTED_PERSON_INTENT_VARIANTS = (
    'уклонение от отбывания ограничения свободы или лишения свободы',
    'уклонение от отбывания ограничения свободы либо лишения свободы',
    'уклонение лица от отбывания наказания в виде ограничения свободы либо лишения свободы',
    'уклонение лица от отбывания наказания в виде ограничения свободы или лишения свободы',
    'нахождение лица в розыске по данным системы видеонаблюдения',
    'нахождение в розыске по камерам',
)

# Не номер статьи и не название конкретного сервера, а реальные общеюридические
# формулировки одной материальной нормы. `_leg_intent_phrase` проверит их по полному
# тексту текущего каталога и выберет первый действительно поддержанный вариант.
_PROTECTED_LIFE_INTENT_VARIANTS = (
    'посягательство на жизнь сотрудника правоохранительного органа',
    'посягательство на жизнь сотрудника государственной организации',
    'посягательство на жизнь сотрудника государственных структур',
    'посягательство на жизнь госсотрудника',
    'посягательство на жизнь госструктур',
    'посягательство на жизнь государственного служащего',
)

# Нейтральная событийная формулировка для query-эмбеддинга. Она сохраняет действие
# и направление ролей, в отличие от мешка слов «гражданский стрелял сотрудник».
_PROTECTED_LIFE_VECTOR_TEXT = 'стрельба по сотруднику правоохранительного органа'

# Только доказанные формы ЧЕЛОВЕКА. Общая slang-map также содержит названия
# организаций (ЛСПД, ДПС, Росгвардия); использовать её префиксы для hard intent
# нельзя — «стрелял по ЛСПД» не доказывает нападение на конкретного сотрудника.
_PROTECTED_OFFICIAL_SLANG_PERSON_FORMS = {
    'гаишник', 'гаишника', 'гаишнику', 'гаишником', 'гаишнике',
    'гаишники', 'гаишников', 'гаишникам', 'гаишниками', 'гаишниках',
    'следак', 'следака', 'следаку', 'следаком', 'следаке',
    'следаки', 'следаков', 'следакам', 'следаками', 'следаках',
    'госник', 'госника', 'госнику', 'госником', 'госнике',
    'госники', 'госников', 'госникам', 'госниками', 'госниках',
    'гостик', 'гостика', 'гостику', 'гостиком', 'гостике',
    'гостики', 'гостиков', 'гостикам', 'гостиками', 'гостиках',
    'госик', 'госика', 'госику', 'госиком', 'госике',
    'госики', 'госиков', 'госикам', 'госиками', 'госиках',
    'мент', 'мента', 'менту', 'ментом', 'менте',
    'менты', 'ментов', 'ментам', 'ментами', 'ментах',
    'росгвардеец', 'росгвардейца', 'росгвардейцу', 'росгвардейцем',
    'росгвардейце', 'росгвардейцы', 'росгвардейцев', 'росгвардейцам',
    'росгвардейцами', 'росгвардейцах',
    'фибовец', 'фибовца', 'фибовцу', 'фибовцем', 'фибовце',
    'фибовцы', 'фибовцев', 'фибовцам', 'фибовцами', 'фибовцах',
    'фибошник', 'фибошника', 'фибошнику', 'фибошником', 'фибошнике',
    'фибошники', 'фибошников', 'фибошникам', 'фибошниками', 'фибошниках',
    'фибовка', 'фибовки', 'фибовке', 'фибовку', 'фибовкой',
    'лспдшник', 'лспдшника', 'лспдшнику', 'лспдшником', 'лспдшнике',
    'лспдшники', 'лспдшников', 'лспдшникам', 'лспдшниками', 'лспдшниках',
    'лспдшница', 'лспдшницы', 'лспдшнице', 'лспдшницу', 'лспдшницей',
    'дпсник', 'дпсника', 'дпснику', 'дпсником', 'дпснике',
    'дпсники', 'дпсников', 'дпсникам', 'дпсниками', 'дпсниках',
    'дпсница', 'дпсницы', 'дпснице', 'дпсницу', 'дпсницей',
    # «легавый» как существительное-человек; женская форма «легавая»
    # намеренно исключена из-за обычного значения «легавая собака».
    'легавый', 'легавого', 'легавому', 'легавым', 'легавом',
    'легавые', 'легавых', 'легавыми',
}
_PROTECTED_OFFICIAL_STEMS = (
    'госслужащ', 'госсотруд', 'военнослуж', 'силовик',
)
_EMPLOYEE_FORMS = {
    'сотрудник', 'сотрудника', 'сотруднику', 'сотрудником', 'сотруднике',
    'сотрудники', 'сотрудников', 'сотрудникам', 'сотрудниками', 'сотрудниках',
}
_POLICE_PERSON_FORMS = {
    'полицейский', 'полицейского', 'полицейскому', 'полицейским', 'полицейском',
    'полицейские', 'полицейских', 'полицейскими',
}
_LAW_ENFORCER_PERSON_FORMS = {
    'правоохранитель', 'правоохранителя', 'правоохранителю', 'правоохранителем',
    'правоохранители', 'правоохранителей', 'правоохранителям', 'правоохранителями',
}
_COP_PERSON_FORMS = {
    'коп', 'копа', 'копу', 'копом', 'копе',
    'копы', 'копов', 'копам', 'копами', 'копах',
}
_PERSON_ROLE_FOLLOWERS = {
    'был', 'была', 'было', 'были',
    'оказался', 'оказалась', 'оказалось', 'оказались',
    'который', 'которая', 'которые',
    'из', 'при', 'после', 'до', 'с', 'со', 'и', 'а', 'но',
    '.', '!', '?', ';', ':',
}
_PROTECTED_EMPLOYEE_QUALIFIER_STEMS = (
    'полици', 'правоохран', 'госструкт', 'государствен',
    'фиб', 'уссс', 'лспд', 'лссд', 'воен',
)
_COMPLETED_DEADLY_ACTION_FORMS = {
    'стрелял', 'стреляла', 'стреляло', 'стреляли',
    'выстрелил', 'выстрелила', 'выстрелило', 'выстрелили',
    'обстрелял', 'обстреляла', 'обстреляло', 'обстреляли',
    'застрелил', 'застрелила', 'застрелило', 'застрелили',
    'расстрелял', 'расстреляла', 'расстреляло', 'расстреляли',
    'убил', 'убила', 'убило', 'убили',
    'покушался', 'покушалась', 'покушалось', 'покушались',
    # Nominative event form is safe in the direct relation «стрельба по ...».
    # Oblique «выстрелом/выстрела» remains ordinary retrieval evidence.
    'стрельба',
}
_COMPLETED_SLANG_DEADLY_FORMS = {
    'замочил', 'замочила', 'замочило', 'замочили',
    'прикончил', 'прикончила', 'прикончило', 'прикончили',
    'укокошил', 'укокошила', 'укокошило', 'укокошили',
    'грохнул', 'грохнула', 'грохнуло', 'грохнули',
    'зарезал', 'зарезала', 'зарезало', 'зарезали',
    'порешил', 'порешила', 'порешило', 'порешили',
}
_PASSIVE_DEADLY_FORMS = {
    'обстрелян', 'обстреляна', 'обстреляно', 'обстреляны',
    'застрелен', 'застрелена', 'застрелено', 'застрелены',
    'расстрелян', 'расстреляна', 'расстреляно', 'расстреляны',
    'убит', 'убита', 'убито', 'убиты',
}
_DIRECT_TARGET_PREPOSITIONS = {'по', 'в', 'на', 'против'}
_NEGATION_MARKERS = {'не', 'никогда', 'никто'}
_DIRECT_OBJECT_MODIFIERS = {'этого', 'того', 'данного', 'одного', 'самого'}
_BARE_OBJECT_ACTION_STEMS = ('застрелил', 'расстрелял', 'обстрелял', 'убил')
_PASSIVE_LINK_WORDS = {
    'был', 'была', 'было', 'были',
    'оказался', 'оказалась', 'оказалось', 'оказались',
    'вчера', 'сегодня', 'недавно', 'ранее',
    'утром', 'днем', 'вечером', 'ночью', 'внезапно',
}
_CLAUSE_BOUNDARIES = {
    'а', 'но', 'затем', 'потом', 'когда', 'который', 'которая', 'которые',
    '.', ',', '!', '?', ';', ':', '—', '–',
}
_COMPLETED_DIRECTED_HARM_FORMS = {
    'угрожал', 'угрожала', 'угрожало', 'угрожали',
    'пригрозил', 'пригрозила', 'пригрозило', 'пригрозили',
    'оскорбил', 'оскорбила', 'оскорбило', 'оскорбили',
    'обматерил', 'обматерила', 'обматерило', 'обматерили',
    'напал', 'напала', 'напало', 'напали',
    'избил', 'избила', 'избило', 'избили',
    'ударил', 'ударила', 'ударило', 'ударили',
}
_HARM_OBJECT_BRIDGES = {
    'расправой', 'убийством', 'насилием', 'избиением',
}
_DWELLING_THEFT_ACTION_FORMS = {
    'вынес', 'вынесла', 'вынесли',
    'обчистил', 'обчистила', 'обчистили',
}
_DWELLING_OBJECT_FORMS = {
    'хату', 'квартиру', 'дом', 'жилище',
}
_DWELLING_OBJECT_MODIFIERS = {
    'всю', 'весь', 'чужую', 'чужой', 'эту', 'этот',
    'его', 'ее', 'их',
}
_DWELLING_DISPOSITION_PATTERNS = {
    ('на', 'торги'),
    ('на', 'аукцион'),
    ('на', 'продажу'),
    ('на', 'реализацию'),
    ('из', 'залога'),
    ('из', 'реестра'),
    ('из', 'описи'),
    ('из', 'кадастра'),
    ('из', 'перечня'),
    ('по', 'решению'),
    ('по', 'постановлению'),
    ('по', 'исполнительному'),
    ('за', 'долги'),
    ('в', 'счет'),
    ('в', 'счёт'),
    ('в', 'натуре'),
    ('в', 'отдельный'),
    ('в', 'отдельную'),
    ('в', 'собственность'),
    ('на', 'баланс'),
    ('на', 'учет'),
    ('на', 'учёт'),
}
_DWELLING_LEGAL_ACTOR_STEMS = (
    'суд', 'пристав', 'банк', 'кредитор', 'исполнител',
)
_NON_DWELLING_HOME_FOLLOWERS = {
    'культуры', 'престарелых', 'творчества', 'правительства',
    'юстиции', 'печати', 'моды', 'быта',
}
_DWELLING_THEFT_LEGAL_VARIANTS = (
    'кража с проникновением в жилище',
    'кража с незаконным проникновением',
    'кража со взломом',
    'кража',
    'тайное хищение чужого имущества',
)


@dataclass(frozen=True)
class _SituationIntent:
    """Высокоточная структура ситуации, а не совпадение одного слова."""

    kind: Literal['protected_life_attack']
    variants: Tuple[str, ...]
    vector_text: str


def _map_slang(tok: str) -> Optional[str]:
    """Сленговый токен → канонический концепт (по префиксу, чтобы ловить склонения). None — не сленг."""
    for stem, canon in _SLANG_STEMS:
        if tok.startswith(stem):
            return canon
    return None

_NUM_UNITS = {
    'ноль': 0, 'один': 1, 'одна': 1, 'два': 2, 'две': 2, 'три': 3, 'четыре': 4,
    'пять': 5, 'шесть': 6, 'семь': 7, 'восемь': 8, 'девять': 9, 'десять': 10,
    'одиннадцать': 11, 'двенадцать': 12, 'тринадцать': 13, 'четырнадцать': 14,
    'пятнадцать': 15, 'шестнадцать': 16, 'семнадцать': 17, 'восемнадцать': 18,
    'девятнадцать': 19,
}
_NUM_TENS = {
    'двадцать': 20, 'тридцать': 30, 'сорок': 40, 'пятьдесят': 50,
    'шестьдесят': 60, 'семьдесят': 70, 'восемьдесят': 80, 'девяносто': 90,
}
_DOT_WORDS = {'точка', 'запятая'}

_TOKEN_RE = re.compile(r'[a-zа-яё0-9.]+|[$%]', re.IGNORECASE)
_SURFACE_TOKEN_RE = re.compile(
    r'[a-zа-яё0-9]+(?:\.[a-zа-яё0-9]+)*|[$%]|[.,!?;:—–]',
    re.IGNORECASE,
)
_CODE_RE = re.compile(r'^\d+(?:\.\d+)*$')
_ATTACHED_SEMANTIC_UNIT_BOUNDARY_RE = re.compile(
    r"(?<=\d)(?=(?:"
    r"(?:кг|мг|км|см|мм|мл|г|м|т|л|р)(?=\b)|"
    r"(?:грам|килограмм|миллиграм|тонн|литр|сантиметр|миллиметр|"
    r"миллилитр|доллар|рубл|сут|час|минут|секунд|"
    r"день|дня|дней|недел|месяц|год|лет|процент|голос|звезд|уров|"
    r"балл|метр|километр|миль|штук|единиц|патрон|растен|семен|"
    r"человек)[a-zа-яё]*\b"
    r"))",
    re.IGNORECASE,
)
_ATTACHED_MONEY_SCALE_BOUNDARY_RE = re.compile(
    r"(?<=\d)(?=[кk](?=\s*(?:\$|доллар[a-zа-яё]*\b)))",
    re.IGNORECASE,
)
_SEMANTIC_UNIT_AFTER_RANGE_RE = re.compile(
    r"^\s*(?:[$%]|(?:кг|мг|км|см|мм|мл|г|м|т|л|р)(?=\W|$)|"
    r"(?:грам|килограмм|миллиграм|тонн|литр|сантиметр|миллиметр|"
    r"миллилитр|доллар|рубл|сут|час|минут|секунд|"
    r"день|дня|дней|недел|месяц|год|лет|процент|голос|звезд|уров|"
    r"балл|метр|километр|миль|штук|единиц|патрон|растен|семен|"
    r"человек)[a-zа-яё]*\b)",
    re.IGNORECASE,
)
_SEMANTIC_SCALE_AND_UNIT_AFTER_RANGE_RE = re.compile(
    r"^\s*(?:тыс|тысяч|тысяча|тысячи|миллион|миллиона|миллионов|"
    r"миллиард|миллиарда|миллиардов|[кk])\s*"
    r"(?:[$%]|(?:кг|мг|км|см|мм|мл|г|м|т|л|р)(?=\W|$)|"
    r"(?:грам|килограмм|миллиграм|тонн|литр|сантиметр|миллиметр|"
    r"миллилитр|доллар|рубл|сут|час|минут|секунд|"
    r"день|дня|дней|недел|месяц|год|лет|процент|голос)[a-zа-яё]*\b)",
    re.IGNORECASE,
)
_MONEY_UNIT_AFTER_NUMBER_RE = re.compile(
    r"^\s*(?:\$|доллар[a-zа-яё]*\b)",
    re.IGNORECASE,
)
_NON_POINT_NUMERIC_QUERY_RE = re.compile(
    r"(?:"
    r"\b(?:не\s+(?:ровно|равно)?|точно\s+не)\s*[$%]?\s*\d"
    r"|"
    r"\b(?:около|примерно|почти|до|от|свыше|более|менее|выше|ниже|"
    r"минимум|максимум|не\s+более|не\s+менее)\s+[$%]?\s*\d"
    r"|"
    r"\b(?:по\s+(?:крайней|меньшей)\s+мере|"
    r"в\s+(?:пределах|районе))\s+"
    r"[$%]?\s*\d"
    r"|"
    r"\b(?:без\s+малого|едва)\s+[$%]?\s*\d"
    r"|"
    r"\bне\s+достиг[a-zа-яё]*\s+[$%]?\s*\d"
    r"|"
    r"[$%]?\s*\d+(?:[.,]\d+)?\s*[$%]?\s*"
    r"(?:[-–]|\b(?:до|по)\b)\s*[$%]?\s*\d+(?:[.,]\d+)?\s*[$%]?"
    r"|"
    r"[$%]?\s*\d+(?:[.,]\d+)?\s*[$%]?\s*"
    r"(?:\+|/|\b(?:и|или)\b)\s*[$%]?\s*\d"
    r"|"
    r"\d+(?:[.,]\d+)?\s+\bплюс\b\s+\d"
    r"|"
    r"[~≈<>≤≥]\s*[$%]?\s*\d"
    r"|"
    r"[$%]?\s*\d+(?:[.,]\d+)?\s*±\s*\d"
    r"|"
    r"±\s*[$%]?\s*\d"
    r"|"
    r"[$%]?\s*\d+(?:[.,]\d+)?\s*[$%]?\s*\+"
    r"|"
    r"\d+\s*,\s*\d"
    r"|"
    r"[$%]?\s*\d+(?:[.,]\d+)?\s*"
    r"(?:(?:тыс|тысяч|миллион|миллиард|[кk])\s*)?"
    r"(?:(?:кг|мг|км|см|мм|мл|г|м|т|л|р)(?=\W|$)|"
    r"(?:грам|килограмм|миллиграм|тонн|литр|сантиметр|миллиметр|"
    r"миллилитр|доллар|сут|час|день|дня|дней|"
    r"процент|голос)[a-zа-яё]*)?\s*"
    r"(?:[-–—]|\b(?:до|по|и|или|либо|плюс)\b)\s*"
    r"[$%]?\s*\d+(?:[.,]\d+)?"
    r"|"
    r"\d+(?:[.,]\d+)?\s*"
    r"(?:(?:кг|мг|км|см|мм|мл|г|м|т|л|р)(?=\W|$)|"
    r"(?:грам|килограмм|миллиграм|тонн|литр|сантиметр|миллиметр|"
    r"миллилитр|доллар|сут|час|день|дня|дней|"
    r"процент|голос)[a-zа-яё]*)?\s*"
    r"(?:около|примерно|приблизительно|ориентировочно|"
    r"максимум|минимум|порядка|"
    r"плюс\s*[-–]?\s*минус|с\s+половиной|"
    r"(?:и(?:\s*/?\s*или)?|или|либо)\s+"
    r"(?:более|выше|меньше|ниже)|"
    r"не\s+(?:более|менее)|"
    r"с\s+(?:лишним|небольшим))\b"
    r")",
    re.IGNORECASE,
)
_COMPACT_DECIMAL_COMMA_RE = re.compile(r"\d+,\d+")
_TYPED_DECIMAL_COMMA_PAIR_RE = re.compile(
    r"(?P<lower>\d+,\d+)"
    r"(?P<middle>\s*(?:[-–—]|\b(?:до|по)\b)\s*(?:[$%]\s*)?)"
    r"(?P<upper>\d+,\d+)",
    re.IGNORECASE,
)


def _normalize_typed_decimal_commas(text: str) -> str:
    """Convert only a decimal value tied to a known semantic unit.

    Compact structural lists such as ``статьи 1,2,3`` deliberately stay
    comma-separated and are tokenised as three legacy code candidates.
    """

    def canonical(raw: str, *, money_context: bool) -> str:
        whole, fraction = raw.split(",", 1)
        if money_context and len(fraction) == 3:
            return whole + fraction
        fraction = fraction.rstrip("0") or "0"
        return f"{whole}.{fraction}"

    def replace_pair(match: "re.Match[str]") -> str:
        tail = match.string[match.end():]
        head = match.string[:match.start()]
        has_suffix_unit = bool(_SEMANTIC_UNIT_AFTER_RANGE_RE.match(tail))
        has_prefix_unit = head.rstrip().endswith(("$", "%"))
        if not has_suffix_unit and not has_prefix_unit:
            return match.group(0)
        money_context = (
            head.rstrip().endswith("$")
            or bool(_MONEY_UNIT_AFTER_NUMBER_RE.match(tail))
        )
        return (
            canonical(match.group("lower"), money_context=money_context)
            + match.group("middle")
            + canonical(match.group("upper"), money_context=money_context)
        )

    text = _TYPED_DECIMAL_COMMA_PAIR_RE.sub(replace_pair, text)

    def replace(match: "re.Match[str]") -> str:
        tail = match.string[match.end():]
        head = match.string[:match.start()]
        if tail.lstrip().startswith(",") or head.rstrip().endswith(","):
            return match.group(0)
        has_suffix_unit = bool(_SEMANTIC_UNIT_AFTER_RANGE_RE.match(tail))
        has_prefix_unit = head.rstrip().endswith(("$", "%"))
        if not has_suffix_unit and not has_prefix_unit:
            return match.group(0)
        money_context = (
            head.rstrip().endswith("$")
            or bool(_MONEY_UNIT_AFTER_NUMBER_RE.match(tail))
        )
        return canonical(match.group(0), money_context=money_context)

    return _COMPACT_DECIMAL_COMMA_RE.sub(replace, text)


def _words_to_digits_with_surfaces(
    tokens: List[str],
) -> Tuple[List[str], List[List[str]]]:
    """Числительные → цифры, сохраняя исходные словоформы каждого результата."""

    out: List[str] = []
    surfaces: List[List[str]] = []
    i = 0
    while i < len(tokens):
        t = tokens[i]
        num: Optional[int] = None
        consumed = [t]
        if t in _NUM_TENS:
            num = _NUM_TENS[t]
            if i + 1 < len(tokens) and tokens[i + 1] in _NUM_UNITS and _NUM_UNITS[tokens[i + 1]] < 10:
                num += _NUM_UNITS[tokens[i + 1]]
                consumed.append(tokens[i + 1])
                i += 1
        elif t in _NUM_UNITS:
            num = _NUM_UNITS[t]
        if num is None:
            # Цифровой токен тоже склеиваем через точку: «12 точка 1» (STT отдаёт цифры) → «12.1»
            if _CODE_RE.match(t) and len(out) >= 2 and out[-1] == '.' and _CODE_RE.match(out[-2]):
                out[-2] = f"{out[-2]}.{t}"
                surfaces[-2].extend(surfaces[-1])
                surfaces[-2].append(t)
                out.pop()
                surfaces.pop()
            else:
                out.append(t)
                surfaces.append([t])
            i += 1
            continue
        # «12 точка 1» / «12 . 1» → «12.1» (склейка с предыдущим числом через точку)
        if len(out) >= 2 and out[-1] == '.' and _CODE_RE.match(out[-2]):
            out[-2] = f"{out[-2]}.{num}"
            surfaces[-2].extend(surfaces[-1])
            surfaces[-2].extend(consumed)
            out.pop()
            surfaces.pop()
        else:
            out.append(str(num))
            surfaces.append(consumed)
        i += 1
    return out, surfaces


def _words_to_digits(tokens: List[str]) -> List[str]:
    """Числительные → цифры со склейкой: «двадцать один» → «21»."""

    return _words_to_digits_with_surfaces(tokens)[0]


_SPOKEN_NUMBER_OBLIQUE_SURFACES = frozenset(
    {
        "одного", "одной", "одному", "одну", "одним", "одном",
        "двух", "двум", "двумя",
        "трех", "трёх", "трем", "трём", "тремя",
        "четырех", "четырёх", "четырем", "четырём", "четырьмя",
        "пяти", "шести", "семи", "восьми", "девяти", "десяти",
        "одиннадцати", "двенадцати", "тринадцати", "четырнадцати",
        "пятнадцати", "шестнадцати", "семнадцати", "восемнадцати",
        "девятнадцати", "двадцати", "тридцати", "сорока",
        "пятидесяти", "шестидесяти", "семидесяти",
        "восьмидесяти", "девяноста",
    }
)
_SPOKEN_NUMBER_SURFACE_PATTERN = "(?:" + "|".join(
    re.escape(word)
    for word in sorted(
        (
            set(_NUM_UNITS)
            | set(_NUM_TENS)
            | set(_SPOKEN_NUMBER_OBLIQUE_SURFACES)
        ),
        key=len,
        reverse=True,
    )
) + ")"


def _leading_number_surface_match(
    original: str,
    code: str,
    *,
    literal: bool,
    spoken_surfaces: List[str],
) -> Optional["re.Match[str]"]:
    """Locate only the proven first written/spoken number surface."""

    if literal:
        pattern = re.escape(code).replace(r"\.", r"[.,]")
    elif spoken_surfaces:
        pattern = r"\s+".join(
            re.escape(surface)
            for surface in spoken_surfaces
        )
    else:
        return None
    return re.search(
        rf"(?<![\w.]){pattern}(?![\w.])",
        original,
        re.IGNORECASE,
    )


def _leading_number_is_nonpoint_expression(
    original: str,
    match: "re.Match[str]",
) -> bool:
    """Reject lossy arithmetic/range syntax tied to the leading number.

    The catalog rescue removes punctuation in SQL.  Without this local guard,
    ``10 ± 2 грамм`` could be mistaken for code 10 + title ``2 грамм``.
    Only syntax immediately surrounding the leading surface is inspected, so
    a real title containing a later time/range remains eligible.
    """

    before = original[:match.start()].rstrip()
    after = original[match.end():]
    if re.search(r"[~≈<>≤≥=±+\-*/×÷]\s*$", before):
        return True
    if re.match(r"\s*(?:±|\+)", after):
        return True

    next_number = (
        rf"(?:[$%]\s*)?"
        rf"(?:\d+(?:[.,]\d+)*|{_SPOKEN_NUMBER_SURFACE_PATTERN})(?!\w)"
    )
    if re.match(
        rf"\s*(?:[-–—/,:;*×÷]|\.\.{{2,}})\s*{next_number}",
        after,
        re.IGNORECASE,
    ):
        return True
    return re.match(
        rf"\s*(?:"
        rf"плюс(?:\s*[-–]?\s*минус)?|"
        rf"минус|и\s*/\s*или|или|либо|и|до|по|не|от|"
        rf"(?:не\s+)?(?:более|менее|выше|ниже|свыше)"
        rf")\s+{next_number}",
        after,
        re.IGNORECASE,
    ) is not None


@dataclass
class NormalizedQuery:
    original: str
    cleaned: str                      # смысловое ядро (для FTS/trgm/словаря)
    numeric_cleaned: str = ""         # то же ядро, но смысловые числительные цифрами
    intent_vector_text: Optional[str] = None
    intent_kind: Optional[str] = None
    tokens: List[str] = field(default_factory=list)
    codes: List[str] = field(default_factory=list)   # кандидаты кода статьи («12.1»)
    # Числа, которые являются частью ситуации (сумма, масса, срок, процент),
    # а не номером статьи. Они обязаны оставаться в cleaned/tokens: иначе
    # «5 грамм», «10 грамм» и «25 грамм» становятся одним запросом.
    semantic_numbers: List[str] = field(default_factory=list)
    # Только буквальная цифра в начале смысловой части может быть shorthand
    # «5 разбой». Словесное «одна партия» таким сигналом не является.
    leading_numeric_literal: bool = False
    # True только когда ОДНА И ТА ЖЕ позиция является и слабым shorthand-кодом,
    # и смысловым числом. Совпадение значений разных чисел сюда не относится.
    ambiguous_code_signal: bool = False
    # Диапазон/приблизительное значение нельзя выдавать за одну точку.
    # Retrieval сохраняет числа, но typed applicability rerank fail-closed
    # отключается, пока нет полноценной interval-vs-interval семантики.
    applicability_query_ambiguous: bool = False
    # Catalog-aware rescue candidate for any leading written or spoken number
    # not already accepted as a code. It is never authoritative by itself: a
    # separate leg may vote only when the same server has this exact code and
    # the complete post-number surface matches that row's title.
    weak_leading_code: Optional[str] = None
    weak_title_remainder: str = ""
    # Части дефисных чисел ДО склейки «10-15»→«10.15»: если склеенного кода в каталоге нет
    # (это был диапазон/перечень, а не поднумерация «10-1»), code-ноги откатываются на них.
    dash_fallback_codes: List[str] = field(default_factory=list)
    legal_variants: List[str] = field(default_factory=list)
    intent_variants: List[str] = field(default_factory=list)
    evidence_facets: List[str] = field(default_factory=list)


def _polarity_flags(text: str) -> Tuple[bool, bool, bool, bool]:
    """Маркеры, которые russian FTS выбрасывает, хотя они меняют смысл нормы."""
    tokens = _TOKEN_RE.findall((text or '').lower().replace('ё', 'е'))
    has_without = 'без' in tokens
    has_not = 'не' in tokens
    pairs = list(zip(tokens, tokens[1:]))
    has_without_purpose = any(left == 'без' and right.startswith('цел') for left, right in pairs)
    has_with_purpose = any(left == 'с' and right.startswith('цел') for left, right in pairs)
    return has_without, has_not, has_without_purpose, has_with_purpose


def _dwelling_theft_variants(surface_tokens: List[str]) -> List[str]:
    """Recognise only completed theft actions with a direct dwelling object."""

    if any(token in _NEGATION_MARKERS for token in surface_tokens):
        return []
    for action_index, action in enumerate(surface_tokens):
        if action not in _DWELLING_THEFT_ACTION_FORMS:
            continue
        for target_index in range(
            action_index + 1,
            min(len(surface_tokens), action_index + 4),
        ):
            target = surface_tokens[target_index]
            if target not in _DWELLING_OBJECT_FORMS:
                continue
            # «вынес мебель из квартиры» is not theft of the dwelling.
            between = surface_tokens[action_index + 1:target_index]
            if (
                'из' in between
                or any(
                    token not in _DWELLING_OBJECT_MODIFIERS
                    for token in between
                )
            ):
                continue
            if action.startswith('вынес') and target not in {'хату', 'квартиру'}:
                continue
            if (
                target == 'дом'
                and target_index + 1 < len(surface_tokens)
                and surface_tokens[target_index + 1]
                in _NON_DWELLING_HOME_FOLLOWERS
            ):
                continue
            if action.startswith('вынес'):
                after = surface_tokens[target_index + 1:target_index + 5]
                if any(
                    tuple(after[offset:offset + 2]) in _DWELLING_DISPOSITION_PATTERNS
                    for offset in range(max(0, len(after) - 1))
                ):
                    continue
                if any(
                    token.startswith(_DWELLING_LEGAL_ACTOR_STEMS)
                    for token in surface_tokens[:action_index]
                ):
                    continue
            return list(_DWELLING_THEFT_LEGAL_VARIANTS)
    return []


def _legal_query_variants(
    tokens: List[str],
    surface_tokens: Optional[List[str]] = None,
) -> List[str]:
    """Дополнительные юридические формулировки без потери исходного запроса."""

    dwelling_variants = _dwelling_theft_variants(surface_tokens or [])
    if dwelling_variants:
        return dwelling_variants

    # Более конкретный общеюридический вариант идёт первым: иначе короткая статья
    # «... государственным транспортным средством» может обойти общий состав только
    # потому, что её заголовок короче. Короткий вариант остаётся fallback для кодексов,
    # где «автомобиль или иное» в названии опущено.
    mentions_vehicle_theft = (
        any(token.startswith('угон') for token in tokens)
        and any(token.startswith(('транспорт', 'автомоб', 'машин')) for token in tokens)
    )
    if mentions_vehicle_theft and 'не' in tokens:
        return []
    has_vehicle_theft = mentions_vehicle_theft
    if has_vehicle_theft:
        generic_vehicle_stems = ('угон', 'транспорт', 'средств', 'автомоб', 'машин')
        qualifiers = [
            token for token in tokens
            if not token.startswith(generic_vehicle_stems)
        ]
        suffix = (' ' + ' '.join(qualifiers)) if qualifiers else ''
        return [
            'неправомерное завладение автомобилем или иным транспортным средством' + suffix,
            'неправомерное завладение транспортным средством' + suffix,
        ]

    mapped: List[str] = []
    changed = False
    for token in tokens:
        replacement: Optional[Tuple[str, ...]] = None
        for stem, equivalent in _LEGAL_EQUIVALENT_STEMS:
            if token.startswith(stem):
                replacement = equivalent
                break
        if replacement:
            mapped.extend(replacement)
            changed = True
        else:
            mapped.append(token)
    variant = ' '.join(mapped).strip()
    original = ' '.join(tokens).strip()
    return [variant] if changed and variant and variant != original else []


def _is_protected_official_at(tokens: List[str], index: int) -> bool:
    if index < 0 or index >= len(tokens):
        return False
    token = tokens[index]
    if token in _PROTECTED_OFFICIAL_SLANG_PERSON_FORMS:
        return True
    if token in _COP_PERSON_FORMS:
        return True
    if token in _POLICE_PERSON_FORMS or token in _LAW_ENFORCER_PERSON_FORMS:
        return (
            index + 1 == len(tokens)
            or tokens[index + 1] in _PERSON_ROLE_FOLLOWERS
        )
    if token.startswith(_PROTECTED_OFFICIAL_STEMS):
        return True
    if token in _EMPLOYEE_FORMS and index + 1 < len(tokens):
        return tokens[index + 1].startswith(_PROTECTED_EMPLOYEE_QUALIFIER_STEMS)
    if token.startswith('государствен') and index + 1 < len(tokens):
        return tokens[index + 1].startswith(('служащ', 'сотрудник'))
    if token.startswith('представител') and index + 1 < len(tokens):
        return tokens[index + 1].startswith('власт')
    return False


_NONASSERTED_ACTION_EXACT = frozenset(
    {
        'возможно', 'вероятно', 'предположительно', 'якобы',
        'неизвестно', 'неверно', 'ошибочно', 'ложно',
        'мог', 'могла', 'могло', 'могли',
        'может', 'могут', 'будет',
        'подозревается', 'подозревался', 'подозревалась',
        'подозревают', 'подозревали',
    }
)
_NONASSERTED_EVENT_LABELS = frozenset(
    {
        'план', 'риск', 'угроза', 'угрозы', 'угрозу', 'угрозой',
        'учение', 'учения', 'учебная', 'учебный', 'учебное', 'учебные',
        'тренировка', 'тренировки', 'тренировкой',
        'репетиция', 'репетиции', 'репетицией',
        'имитация', 'имитации', 'имитацией',
    }
)
_NONASSERTED_ACTION_STEMS = (
    'планир', 'предотвращ', 'отмен', 'запрещ',
    'намерева', 'ожида', 'предсто', 'предполага', 'намеч',
    'собирал', 'возмож', 'вероят', 'обсужда', 'отрица',
    'расследу', 'сомн', 'маловероят', 'опроверг',
)


def _contains_nonasserted_action_cue(
    tokens: List[str],
    *,
    include_event_labels: bool = False,
) -> bool:
    if any(
        token in _NONASSERTED_ACTION_EXACT
        or (
            include_event_labels
            and token in _NONASSERTED_EVENT_LABELS
        )
        or token.startswith(_NONASSERTED_ACTION_STEMS)
        for token in tokens
    ):
        return True
    if any(
        (left == 'нет' and right.startswith(('основан', 'доказатель')))
        or (
            left.startswith('отсутств')
            and right.startswith(('основан', 'доказатель'))
        )
        for left, right in zip(tokens, tokens[1:])
    ):
        return True
    if any(
        (left == 'вряд' and right == 'ли')
        or (left == 'по' and right.startswith('слух'))
        or (left == 'не' and right == 'то')
        or (
            left == 'не'
            and right.startswith(
                (
                    'факт', 'уверен', 'ясно', 'известно',
                    'доказан', 'подтвержден', 'исключ',
                    'состоя', 'произош',
                )
            )
        )
        for left, right in zip(tokens, tokens[1:])
    ):
        return True
    proof_stems = ('установ', 'доказ', 'подтверд', 'подтвержд', 'зафиксир')
    for index, token in enumerate(tokens):
        if token == 'не' and any(
            candidate.startswith(proof_stems)
            for candidate in tokens[index + 1:index + 5]
        ):
            return True
        if (
            token == 'нет'
            and index + 1 < len(tokens)
            and tokens[index + 1].startswith(
                ('доказатель', 'подтвержден')
            )
        ):
            return True
    return False


def _contains_positive_assertion_cue(tokens: List[str]) -> bool:
    for index, token in enumerate(tokens):
        if not token.startswith(
            ('установ', 'доказал', 'доказан', 'подтвердил', 'зафиксир')
        ):
            continue
        prefix = tokens[:index]
        if (
            'не' in prefix
            or any(
                item in {
                    'якобы', 'предположительно', 'возможно',
                    'вероятно', 'маловероятно',
                }
                for item in prefix
            )
            or any(
                (left == 'по' and right.startswith('слух'))
                or (left == 'вряд' and right == 'ли')
                for left, right in zip(prefix, prefix[1:])
            )
        ):
            continue
        return True
    return False


_POST_ACTION_DISPROOF_PASSIVES = frozenset(
    {
        'опровергнут', 'опровергнута', 'опровергнуто', 'опровергнуты',
        'ложен', 'ложна', 'ложно', 'ложны',
        'ложным', 'ложной', 'ложными',
    }
)
_POST_ACTION_PROOF_PASSIVES = frozenset(
    {
        'доказан', 'доказана', 'доказано', 'доказаны',
        'подтвержден', 'подтверждена', 'подтверждено', 'подтверждены',
        'установлен', 'установлена', 'установлено', 'установлены',
        'зафиксирован', 'зафиксирована', 'зафиксировано', 'зафиксированы',
        'исключен', 'исключена', 'исключено', 'исключены',
    }
)


def _contains_post_action_disproof(
    tokens: List[str],
    *,
    include_hearsay: bool,
    allow_bare_status: bool,
) -> bool:
    words = [
        token
        for token in tokens
        if token not in _CLAUSE_BOUNDARIES
    ]
    refers_to_event = any(
        token in {'это', 'событие', 'случившееся', 'факт'}
        for token in words
    )
    if include_hearsay and any(
        left == 'по' and right.startswith('слух')
        for left, right in zip(words, words[1:])
    ):
        return True
    if any(
        left == 'или' and right == 'нет'
        for left, right in zip(words, words[1:])
    ):
        return True
    if any(
        (left.startswith('доказатель') and right == 'нет')
        or (left == 'нет' and right.startswith('доказатель'))
        for left, right in zip(words, words[1:])
    ):
        return True
    if any(
        words[index].startswith('основан')
        and words[index + 1:index + 4] in (
            ['нет'],
            ['так', 'считать', 'нет'],
            ['считать', 'нет'],
        )
        for index in range(len(words))
    ):
        return True
    if any(
        left == 'не'
        and right.startswith('соответств')
        and index + 2 < len(words)
        and words[index + 2].startswith('действительн')
        and (
            refers_to_event
            or (allow_bare_status and index <= 1)
        )
        for index, (left, right) in enumerate(zip(words, words[1:]))
    ):
        return True
    for index, token in enumerate(words):
        status_is_local = refers_to_event or (
            allow_bare_status and index <= 2
        )
        if token in _POST_ACTION_DISPROOF_PASSIVES and status_is_local:
            return True
        if token not in _POST_ACTION_PROOF_PASSIVES or not status_is_local:
            continue
        if 'не' in words[max(0, index - 3):index]:
            return True
    return False


def _action_is_nonasserted(
    tokens: List[str],
    action_index: int,
) -> bool:
    """Scope modality/denial to the clause containing this exact action."""

    left = action_index
    while left > 0 and tokens[left - 1] not in _CLAUSE_BOUNDARIES:
        left -= 1
    right = action_index + 1
    while right < len(tokens) and tokens[right] not in _CLAUSE_BOUNDARIES:
        right += 1

    before = tokens[left:action_index]
    after = tokens[action_index + 1:right]
    token = tokens[action_index]
    if any(
        left_token == 'не' and right_token == 'то'
        for left_token, right_token in zip(before, before[1:])
    ):
        return True
    if (
        _contains_nonasserted_action_cue(
            before,
            include_event_labels=(token == 'стрельба'),
        )
        and not _contains_positive_assertion_cue(before)
    ):
        return True
    # Short modal adverbs may follow either a finite verb or an event noun.
    if any(
        item in {
            'возможно', 'вероятно', 'предположительно',
            'якобы', 'неизвестно',
        }
        for item in after
    ):
        return True
    if _contains_post_action_disproof(
        after,
        include_hearsay=True,
        allow_bare_status=True,
    ):
        return True
    # Event nouns do not encode tense. Their following predicate is therefore
    # part of the assertion proof (planned/prevented/discussed vs occurred).
    if token == 'стрельба' and _contains_nonasserted_action_cue(
        after,
        include_event_labels=True,
    ):
        return True

    # Reporting/denial before a comma or a label before a colon scopes into
    # the following ``что ...`` clause / event noun. A contrasting ``но``
    # starts a fresh assertion and is intentionally not inherited.
    if left > 0 and tokens[left - 1] in {',', ':'}:
        previous_right = left - 1
        previous_left = previous_right
        while (
            previous_left > 0
            and tokens[previous_left - 1] not in _CLAUSE_BOUNDARIES
        ):
            previous_left -= 1
        previous = tokens[previous_left:previous_right]
        if _contains_nonasserted_action_cue(
            previous,
            include_event_labels=True,
        ):
            return True
    following_start = right
    while (
        following_start < len(tokens)
        and tokens[following_start] in _CLAUSE_BOUNDARIES
    ):
        following_start += 1
    following_end = following_start
    while (
        following_end < len(tokens)
        and tokens[following_end] not in _CLAUSE_BOUNDARIES
    ):
        following_end += 1
    following = tokens[following_start:following_end]
    if (
        following[:1]
        and following[0] in {
            'возможно', 'вероятно', 'предположительно',
            'якобы', 'неизвестно',
        }
        and (
            len(following) == 1
            or (
                len(following) == 2
                and following[1] in {
                    'это', 'событие', 'случившееся', 'так',
                }
            )
        )
    ):
        return True
    if following == ['по', 'слухам']:
        return True
    if _contains_post_action_disproof(
        following,
        include_hearsay=False,
        allow_bare_status=True,
    ):
        return True
    return False


def _is_deadly_action_at(tokens: List[str], index: int) -> bool:
    # Инфинитив описывает возможность/намерение/приказ, но не доказывает событие:
    # «отказался стрелять» не должен получать ту же статью, что «стрелял».
    if _is_deadly_infinitive_at(tokens, index):
        return False
    token = tokens[index]
    if _action_is_nonasserted(tokens, index):
        return False
    if token in _COMPLETED_SLANG_DEADLY_FORMS:
        return True
    if token in _COMPLETED_DEADLY_ACTION_FORMS:
        return True
    if token in _PASSIVE_DEADLY_FORMS:
        return True
    # «открыл/открыла/открыли огонь» — одно составное действие.
    return (
        token in {'открыл', 'открыла', 'открыло', 'открыли'}
        and index + 1 < len(tokens)
        and tokens[index + 1].startswith('огон')
    )


def _is_passive_deadly_action(token: str) -> bool:
    return token in _PASSIVE_DEADLY_FORMS


def _is_instrumental_protected_official(tokens: List[str], index: int) -> bool:
    """Instrumental official before a passive verb is the agent, not victim."""

    if not _is_protected_official_at(tokens, index):
        return False
    return tokens[index].endswith(
        ('ом', 'ем', 'ым', 'им', 'ами', 'ями', 'ими')
    )


def _is_deadly_infinitive_at(tokens: List[str], index: int) -> bool:
    token = tokens[index]
    if token in {
        'стрелять', 'выстрелить', 'обстрелять', 'застрелить',
        'расстрелять', 'убить',
    }:
        return True
    return (
        token in {'открыть', 'открывать'}
        and index + 1 < len(tokens)
        and tokens[index + 1].startswith('огон')
    )


def _pair_is_negated(tokens: List[str], action_index: int, target_index: int) -> bool:
    # Смотрим отрицание перед действием и между действием/целью, но не считаем
    # позднее «без лицензии» отрицанием уже описанной стрельбы.
    left = max(0, min(action_index, target_index) - 2)
    right = max(action_index, target_index) + 1
    for index in range(left, right):
        token = tokens[index]
        if token in _NEGATION_MARKERS:
            return True
        if (
            token == 'без'
            and index + 1 < len(tokens)
            and _is_deadly_action_at(tokens, index + 1)
        ):
            return True
    return False


def _is_procedural_violence_query(tokens: List[str]) -> bool:
    """Вопрос о праве/порядке не должен превращаться в обвинительную статью."""
    infinitive = any(_is_deadly_infinitive_at(tokens, index) for index in range(len(tokens)))
    procedural_cue = (
        any(
            token in {
                'можно', 'как', 'когда', 'право', 'вправе', 'допускается',
            }
            for token in tokens
        )
        or any(
            token.startswith(('поряд', 'разреш', 'правомер', 'законн'))
            for token in tokens
        )
    )
    if infinitive and procedural_cue:
        return True
    if 'ли' in tokens and any(
        token.startswith(('правомер', 'законн'))
        for token in tokens
    ):
        return True
    if 'можно' in tokens and any(token.startswith('задерж') for token in tokens):
        return True
    if (
        any(token.startswith('срок') for token in tokens)
        and any(token.startswith('задерж') for token in tokens)
    ):
        return True
    return False


def _has_direct_protected_target(
    tokens: List[str],
    action_index: int,
) -> bool:
    # Активная конструкция: «стрелял по/в сотрудника», включая
    # «открыл огонь по сотруднику» и короткие обстоятельства между словами.
    for prep_index in range(action_index + 1, min(len(tokens), action_index + 6)):
        if tokens[prep_index] in _CLAUSE_BOUNDARIES:
            break
        if tokens[prep_index] not in _DIRECT_TARGET_PREPOSITIONS:
            continue
        target_index = prep_index + 1
        if (
            _is_protected_official_at(tokens, target_index)
            and not _pair_is_negated(tokens, action_index, target_index)
        ):
            return True

    # Беспредложный прямой объект: «застрелил сотрудника». Разрешаем только
    # безопасные указательные слова, чтобы «стрелял рядом с госником» не сработало.
    if (
        tokens[action_index].startswith(_BARE_OBJECT_ACTION_STEMS)
        or tokens[action_index] in _COMPLETED_SLANG_DEADLY_FORMS
    ):
        for target_index in range(action_index + 1, min(len(tokens), action_index + 4)):
            between = tokens[action_index + 1:target_index]
            if any(token not in _DIRECT_OBJECT_MODIFIERS for token in between):
                continue
            if (
                _is_protected_official_at(tokens, target_index)
                and not _pair_is_negated(tokens, action_index, target_index)
            ):
                return True

    # Инверсия: «в госника стрелял гражданский».
    for prep_index in range(max(0, action_index - 5), action_index):
        if any(
            token in _CLAUSE_BOUNDARIES
            for token in tokens[prep_index + 1:action_index]
        ):
            continue
        if tokens[prep_index] not in _DIRECT_TARGET_PREPOSITIONS:
            continue
        target_index = prep_index + 1
        if target_index >= action_index:
            continue
        if (
            _is_protected_official_at(tokens, target_index)
            and not _pair_is_negated(tokens, action_index, target_index)
        ):
            return True

    # Пассив: «госник был обстрелян гражданским». Для активного
    # «госник обстрелял гражданского» этот путь намеренно закрыт.
    if _is_passive_deadly_action(tokens[action_index]):
        for target_index in range(max(0, action_index - 4), action_index):
            between = tokens[target_index + 1:action_index]
            if any(
                token not in _PASSIVE_LINK_WORDS
                and not token.startswith(('полиц', 'правоохран', 'служащ', 'власт'))
                for token in between
            ):
                continue
            if (
                _is_protected_official_at(tokens, target_index)
                and not _is_instrumental_protected_official(
                    tokens,
                    target_index,
                )
                and not _pair_is_negated(tokens, action_index, target_index)
            ):
                return True
    return False


def _extract_situation_intent(tokens: List[str]) -> Optional[_SituationIntent]:
    """Извлекает high-precision `действие → защищённая цель` из исходного порядка."""
    if _is_procedural_violence_query(tokens):
        return None
    for action_index in range(len(tokens)):
        if not _is_deadly_action_at(tokens, action_index):
            continue
        if _has_direct_protected_target(tokens, action_index):
            return _SituationIntent(
                kind='protected_life_attack',
                variants=_PROTECTED_LIFE_INTENT_VARIANTS,
                vector_text=_PROTECTED_LIFE_VECTOR_TEXT,
            )
    return None


def _has_directed_protected_harm(tokens: List[str]) -> bool:
    """High-precision role facet for a completed harmful act against an official."""

    if _is_procedural_violence_query(tokens):
        return False
    for action_index, action in enumerate(tokens):
        if _is_deadly_action_at(tokens, action_index):
            if _has_direct_protected_target(tokens, action_index):
                return True
            continue
        if action not in _COMPLETED_DIRECTED_HARM_FORMS:
            continue
        if _action_is_nonasserted(tokens, action_index):
            continue

        for prep_index in range(
            action_index + 1,
            min(len(tokens), action_index + 5),
        ):
            if tokens[prep_index] in _CLAUSE_BOUNDARIES:
                break
            if tokens[prep_index] not in _DIRECT_TARGET_PREPOSITIONS:
                continue
            target_index = prep_index + 1
            if (
                _is_protected_official_at(tokens, target_index)
                and not _pair_is_negated(tokens, action_index, target_index)
            ):
                return True

        for target_index in range(
            action_index + 1,
            min(len(tokens), action_index + 4),
        ):
            between = tokens[action_index + 1:target_index]
            if any(
                token not in _DIRECT_OBJECT_MODIFIERS
                and token not in _HARM_OBJECT_BRIDGES
                for token in between
            ):
                continue
            if (
                _is_protected_official_at(tokens, target_index)
                and not _pair_is_negated(tokens, action_index, target_index)
            ):
                return True
    return False


def _intent_query_variants(tokens: List[str]) -> List[str]:
    """Строгие варианты материальной нормы для однозначного описания ситуации."""
    # Отрицание меняет смысл на противоположный. Вопросы о порядке объявления/снятия
    # розыска также не должны превращаться в обвинение человека в уклонении.
    if any(marker in tokens for marker in ('не', 'без')):
        return []
    person_forms = {
        'лицо', 'лица', 'лицу', 'лицом', 'лице', 'лиц', 'лицам', 'лицами', 'лицах',
        'люди', 'людей', 'людям', 'людьми', 'людях',
    }

    def is_person(token: str) -> bool:
        return (
            token in person_forms
            or token.startswith('человек')
            or token.startswith('граждан')
        )

    def is_declared_status(token: str) -> bool:
        return (
            token == 'объявлен'
            or token.startswith(('объявленн', 'объявлена', 'объявлено', 'объявлены'))
        )

    has_person = any(is_person(token) for token in tokens)
    has_wanted = any(token.startswith(('розыск', 'разыск')) for token in tokens)
    status_stems = ('наход', 'числ', 'состо', 'разыскива')
    descriptor_stems = ('розыск', 'разыск', 'федерал', 'регион', 'активн')
    service_words = {
        'в', 'на', 'который', 'которая', 'которое', 'которые', 'сейчас', 'данный',
    }

    def is_status_word(token: str) -> bool:
        return token.startswith(status_stems) or is_declared_status(token)

    has_status_word = any(is_status_word(token) for token in tokens)
    person_positions = [index for index, token in enumerate(tokens) if is_person(token)]
    wanted_positions = [
        index for index, token in enumerate(tokens)
        if token.startswith(('розыск', 'разыск'))
    ]
    short_person_in_wanted = any(
        person_index < in_index < wanted_index
        for person_index in person_positions
        for in_index, token in enumerate(tokens)
        if token == 'в'
        for wanted_index in wanted_positions
    )

    # Pin безопасен только для «чистого» описания статуса. Вопросы о правах,
    # задержании, порядке объявления и т.п. остаются обычным гибридным поиском.
    pure_status = all(
        token in service_words
        or is_person(token)
        or is_status_word(token)
        or token.startswith(descriptor_stems)
        for token in tokens
    )
    if has_person and has_wanted and pure_status and (has_status_word or short_person_in_wanted):
        return list(_WANTED_PERSON_INTENT_VARIANTS)
    return []


def normalize_query(raw: str) -> NormalizedQuery:
    """Голосовой/текстовый запрос → нормализованная форма для гибридного поиска."""
    original = (raw or "").strip()[:200]
    lowered = original.lower().replace('ё', 'е')
    # Русская десятичная запятая — часть одного значения, а не два разных
    # числа. Внутри нормализатора используем точку; original не меняется.
    lowered = _normalize_typed_decimal_commas(lowered)
    # Forum-style ellipsis between two numeric endpoints is a range
    # separator, not a malformed dotted article code.
    lowered = re.sub(r"(?<=\d)\.{2,}(?=\d)", " до ", lowered)
    applicability_query_ambiguous = bool(
        _NON_POINT_NUMERIC_QUERY_RE.search(lowered)
    )
    # Дефисная поднумерация статей «10-1» (УК/УПК RMRP) → канонический код «10.1»:
    # каталог хранит такие коды с точкой (normalize_header), а _TOKEN_RE иначе
    # разрезал бы «10-1» на два отдельных кода «10» и «1». Части дефисных чисел
    # запоминаем как fallback: «статьи 10-15» — диапазон, склейка «10.15» в каталоге
    # не найдётся, и code-ноги вернутся к прежнему поведению (коды 10 и 15).
    # Склейка и захват fallback — ОДИН регексп: смешанные группы с точкой («10-1.5»)
    # не трогаем вовсе (прежнее поведение — split по дефису на «10» и «1.5»).
    dash_fallback: List[str] = []

    def _join_dash_group(m: "re.Match[str]") -> str:
        nonlocal applicability_query_ambiguous
        tail = m.string[m.end():]
        head = m.string[:m.start()]
        number_marker = (
            r"(?:(?:№|n(?:o)?)(?:\s*/\s*(?:n(?:o)?))?\.?\s*)?"
        )
        plural_reference = re.search(
            r"\b(?:"
            r"статьи|статей|статьям|статьями|статьях|"
            r"главы|глав|главам|главами|главах|"
            r"разделы|разделов|разделам|разделами|разделах|"
            r"пункты|пунктов|пунктам|пунктами|пунктах|"
            r"части|частей|частям|частями|частях|"
            r"томы|томов|томам|томами|томах"
            r")\s*:?\s*"
            + number_marker
            + r"(?:(?:с|от)\s+)?$",
            head,
            re.IGNORECASE,
        )
        parts = [
            int(part)
            for part in re.split(r"[-–]", m.group(0))
        ]
        abbreviated_reference = re.search(
            r"\bст\.\s*:?\s*"
            + number_marker
            + r"(?:(?:с|от)\s+)?$",
            head,
            re.IGNORECASE,
        )
        if plural_reference or (
            abbreviated_reference
            and len(parts) == 2
            and parts[0] <= parts[1]
        ):
            # A plural structural marker proves a list/range. Never let a
            # real code such as 5.10 steal ``статьи 5-10``.
            return m.group(0)
        has_typed_unit = bool(_SEMANTIC_UNIT_AFTER_RANGE_RE.match(tail))
        has_typed_unit = has_typed_unit or bool(
            _SEMANTIC_SCALE_AND_UNIT_AFTER_RANGE_RE.match(tail)
        )
        has_prefixed_unit = (
            m.start() > 0 and m.string[m.start() - 1] in "$%"
        )
        if has_typed_unit or has_prefixed_unit:
            applicability_query_ambiguous = True
            # Это диапазон фактов ситуации, не код статьи. Оставляем дефис:
            # tokenizer безопасно разделит границы, а typed point-rerank ниже
            # будет отключён.
            return m.group(0)
        dash_fallback.extend(p for p in re.split(r'[-–]', m.group(0)) if p)
        return re.sub(r'[-–]', '.', m.group(0))

    lowered = re.sub(r'(?<![\d.])\d+(?:[-–]\d+)+(?![\d.])', _join_dash_group, lowered)
    # Разделяем только доказанные numeric+unit формы. Общий alnum split сломал
    # бы алиасы/названия вроде GTA5RP, поэтому `20г`, `5000долларов`,
    # `24часа` обрабатываются узкой грамматикой известных единиц.
    lowered = _ATTACHED_MONEY_SCALE_BOUNDARY_RE.sub(" ", lowered)
    lowered = _ATTACHED_SEMANTIC_UNIT_BOUNDARY_RE.sub(" ", lowered)
    surface_tokens = [
        token
        for token in _SURFACE_TOKEN_RE.findall(lowered)
        if _CODE_RE.fullmatch(token) is None and token not in {"$", "%"}
    ]
    tokens = _TOKEN_RE.findall(lowered)
    tokens = ['.' if t in _DOT_WORDS else t.strip('.') or '.' for t in tokens]
    tokens = [t for t in tokens if t]
    pre_numeric_content = [
        token
        for token in tokens
        if token != "." and token not in _WRAPPER_WORDS
    ]
    leading_numeric_literal = bool(
        pre_numeric_content
        and _CODE_RE.fullmatch(pre_numeric_content[0])
    )
    tokens, token_surfaces = _words_to_digits_with_surfaces(tokens)
    pre_mapping_tokens = list(tokens)
    pre_mapping_count_reasons = {
        index: reason
        for index, token in enumerate(tokens)
        if _CODE_RE.fullmatch(token)
        if (reason := _definite_leading_count_reason(tokens, index))
        is not None
    }
    pre_mapping_definite_count_indexes = set(
        pre_mapping_count_reasons
    )
    pre_mapping_content_indexes = [
        index
        for index, token in enumerate(tokens)
        if token != "." and token not in _WRAPPER_WORDS
    ]
    pre_mapping_leading_index = (
        pre_mapping_content_indexes[0]
        if pre_mapping_content_indexes
        and _CODE_RE.fullmatch(tokens[pre_mapping_content_indexes[0]])
        else None
    )
    leading_number_surfaces = (
        token_surfaces[pre_mapping_leading_index]
        if pre_mapping_leading_index is not None
        else []
    )
    leading_spoken_number = bool(
        leading_number_surfaces
        and all(
            surface in _NUM_UNITS or surface in _NUM_TENS
            for surface in leading_number_surfaces
        )
    )
    pre_mapping_semantic_indexes = {
        index
        for index, token in enumerate(tokens)
        if _CODE_RE.fullmatch(token)
        and (
            _is_semantic_number_context(tokens, index)
            or index in pre_mapping_definite_count_indexes
        )
    }
    # Role-parser работает по исходному порядку и формам слов: после slang-map
    # «госнику» станет несклоняемым «сотрудник», но для определения цели нам важна
    # сама исходная конструкция «по госнику».
    # Сленг → канонический концепт ДО выделения ядра: «госнику»→«сотрудник» уходит в FTS/название/
    # вектор (сильный сигнал), а не в шумные авто-синонимы, и не ломается на склонении.
    # многословный канон («холодное оружие») разбиваем на отдельные токены — иначе склеенный
    # токен с пробелом ломает fts relaxed-OR (регэксп режет пробел → «холодноеоружие»).
    _mapped: List[str] = []
    _mapped_surfaces: List[List[str]] = []
    _mapped_semantic_indexes: set[int] = set()
    _mapped_definite_count_indexes: set[int] = set()
    for _source_index, (_t, _surface) in enumerate(
        zip(tokens, token_surfaces)
    ):
        _mapped_index = len(_mapped)
        _c = _map_slang(_t)
        if _c:
            for _part in _c.split():
                _mapped.append(_part)
                _mapped_surfaces.append([_part])
        else:
            _mapped.append(_t)
            _mapped_surfaces.append(_surface)
            if _source_index in pre_mapping_semantic_indexes:
                _mapped_semantic_indexes.add(_mapped_index)
            if _source_index in pre_mapping_definite_count_indexes:
                _mapped_definite_count_indexes.add(_mapped_index)
    tokens = _mapped
    if any(
        _compound_scaled_measure_at(tokens, index)
        for index, token in enumerate(tokens)
        if _CODE_RE.fullmatch(token)
    ):
        applicability_query_ambiguous = True

    spoken_quantity_indexes = {
        index
        for index, (token, surfaces) in enumerate(
            zip(tokens, _mapped_surfaces)
        )
        if _CODE_RE.fullmatch(token)
        and any(
            surface in _NUM_UNITS or surface in _NUM_TENS
            for surface in surfaces
        )
        and (
            _is_semantic_number_context(tokens, index)
            or (
                index + 1 < len(tokens)
                and tokens[index + 1]
                in (
                    _INVERTED_UNAMBIGUOUS_STRUCTURE_MARKERS
                    | _INVERTED_CONTEXTUAL_STRUCTURE_MARKERS
                )
            )
        )
    }
    code_indexes, semantic_number_indexes, code_only_indexes = _partition_query_numbers(
        tokens,
        allow_leading_dual=(
            leading_numeric_literal
            and not applicability_query_ambiguous
        ),
        spoken_quantity_indexes=spoken_quantity_indexes,
        semantic_hint_indexes=_mapped_semantic_indexes,
        definite_count_indexes=_mapped_definite_count_indexes,
    )
    ambiguous_code_signal = bool(
        code_indexes & semantic_number_indexes
    )
    codes = [token for index, token in enumerate(tokens) if index in code_indexes]
    semantic_numbers = [
        token for index, token in enumerate(tokens)
        if index in semantic_number_indexes
    ]
    if any(
        _semantic_number_is_negated_or_inexact(tokens, index)
        for index in semantic_number_indexes
    ):
        applicability_query_ambiguous = True
    leading_count_reason = (
        pre_mapping_count_reasons.get(pre_mapping_leading_index)
        if pre_mapping_leading_index is not None
        else None
    )
    leading_typed_count = leading_count_reason in {
        "typed_unit",
        "typed_scale",
        "typed_scaled_unit",
    }
    weak_leading_code = (
        pre_mapping_tokens[pre_mapping_leading_index]
        if (
            pre_mapping_leading_index is not None
            and (leading_numeric_literal or leading_spoken_number)
            and not leading_typed_count
            and pre_mapping_tokens[pre_mapping_leading_index]
            not in codes
        )
        else None
    )
    weak_title_remainder = ""
    if weak_leading_code is not None:
        code_surface_match = _leading_number_surface_match(
            original,
            weak_leading_code,
            literal=leading_numeric_literal,
            spoken_surfaces=leading_number_surfaces,
        )
        if (
            code_surface_match is not None
            and _leading_number_is_nonpoint_expression(
                original,
                code_surface_match,
            )
        ):
            weak_leading_code = None
        elif code_surface_match is not None:
            weak_title_remainder = (
                original[code_surface_match.end():]
                .strip()
                .lower()
                .replace("ё", "е")
            )
        elif not leading_numeric_literal:
            # The compressed token retains every spoken surface
            # (``тридцать пять`` -> ``35`` + ["тридцать", "пять"]).
            # Reconstruct the untouched title after that token; do not search
            # for ``35`` in the original because the title itself may contain
            # another numeric value.
            weak_title_remainder = " ".join(
                surface
                for surfaces in token_surfaces[
                    pre_mapping_leading_index + 1:
                ]
                for surface in surfaces
                if surface != "."
            ).strip()
    if not weak_title_remainder:
        weak_leading_code = None
    # Из смыслового ядра удаляем только доказанные номера структуры. Количество,
    # сумма, срок и неоднозначный короткий shorthand («5 разбой») сохраняют число:
    # оно нужно FTS, словарю, trigram и FRIDA для различения порогов.
    numeric_concept = [
        token
        for index, token in enumerate(tokens)
        if token not in _WRAPPER_WORDS
        and token != "."
        and index not in code_only_indexes
    ]
    concept: List[str] = []
    for index, token in enumerate(tokens):
        if (
            token in _WRAPPER_WORDS
            or token == "."
            or index in code_only_indexes
        ):
            continue
        if index in semantic_number_indexes:
            if "." in token:
                # Spoken decimal «двадцать запятая пять» must not collapse to
                # the retrieval surface of integer 25.
                concept.append(token)
                continue
            source_words = [
                part
                for part in _mapped_surfaces[index]
                if part != "."
                and part not in _DOT_WORDS
                and (part in _NUM_UNITS or part in _NUM_TENS)
            ]
            if source_words:
                concept.extend(source_words)
                continue
        concept.append(token)
    cleaned = ' '.join(concept).strip()
    has_authoritative_code = bool(code_only_indexes)
    situation = (
        None
        if has_authoritative_code
        else _extract_situation_intent(surface_tokens)
    )
    evidence_facets = (
        ["государственный", "сотрудник"]
        if (
            not has_authoritative_code
            and _has_directed_protected_harm(surface_tokens)
        )
        else []
    )
    intent_variants = (
        list(situation.variants)
        if situation is not None
        else ([] if codes else _intent_query_variants(concept))
    )
    return NormalizedQuery(
        original=original,
        cleaned=cleaned,
        numeric_cleaned=" ".join(numeric_concept).strip(),
        intent_vector_text=situation.vector_text if situation is not None else None,
        intent_kind=situation.kind if situation is not None else (
            "wanted_person" if intent_variants else None
        ),
        tokens=concept,
        codes=codes,
        semantic_numbers=semantic_numbers,
        leading_numeric_literal=leading_numeric_literal,
        ambiguous_code_signal=ambiguous_code_signal,
        applicability_query_ambiguous=applicability_query_ambiguous,
        weak_leading_code=weak_leading_code,
        weak_title_remainder=weak_title_remainder,
        dash_fallback_codes=[c for c in dash_fallback if c not in codes] if codes else [],
        # Явно названный номер статьи сильнее любой ситуационной интерпретации.
        # Synthetic rewrite здесь только мог бы закрепить другую статью выше code-leg.
        legal_variants=(
            []
            if has_authoritative_code
            else _legal_query_variants(concept, surface_tokens)
        ),
        intent_variants=intent_variants,
        evidence_facets=evidence_facets,
    )


_CODE_KIND_MARKERS = {
    "article": frozenset(
        {
            "статья",
            "статьи",
            "статью",
            "статье",
            "статьей",
            "статьёй",
            "статьями",
            "статьях",
            "статьям",
            "статей",
            "ст",
        }
    ),
    "chapter": frozenset(
        {
            "глава",
            "главы",
            "главу",
            "главе",
            "главой",
            "главами",
            "главах",
            "глав",
            "главам",
        }
    ),
    "section": frozenset(
        {
            "раздел",
            "раздела",
            "разделу",
            "разделе",
            "разделом",
            "разделы",
            "разделам",
            "разделами",
            "разделах",
            "разделов",
        }
    ),
}
_CODE_MARKER_TO_KIND = {
    marker: kind
    for kind, markers in _CODE_KIND_MARKERS.items()
    for marker in markers
}
_CODE_MARKER_LINK_WORDS = frozenset(
    {
        "номер",
        "номера",
        "номеру",
        "номером",
        "номере",
        "ном",
        "под",
        "n",
        "no",
    }
)
_GENERIC_STRUCTURE_CODE_MARKERS = frozenset(
    {
        "пункт",
        "пункта",
        "пункту",
        "пункте",
        "пунктом",
        "пункты",
        "пунктов",
        "пунктам",
        "пунктами",
        "пунктах",
        "п",
        "часть",
        "части",
        "частью",
        "частей",
        "частям",
        "частями",
        "частях",
        "ч",
        "том",
        "тома",
        "тому",
        "томе",
        "томом",
        "томы",
        "томов",
        "номер",
        "номера",
        "номеру",
        "номером",
        "номере",
        "ном",
    }
)
_INVERTED_UNAMBIGUOUS_STRUCTURE_MARKERS = frozenset(
    {
        "статья",
        "статье",
        "статью",
        "статьей",
        "статьёй",
        "глава",
        "главе",
        "главу",
        "главой",
        "раздел",
        "разделе",
        "разделу",
        "разделом",
        "пункт",
        "пункте",
        "пункту",
        "пунктом",
        "часть",
        "частью",
        "том",
        "томе",
        "тому",
        "томом",
    }
)
_INVERTED_CONTEXTUAL_STRUCTURE_MARKERS = frozenset(
    {
        "статьи",
        "главы",
        "разделы",
        "части",
        "пункты",
    }
)
_INVERTED_REFERENCE_PREPOSITIONS = frozenset(
    {
        "в",
        "во",
        "на",
        "по",
        "согласно",
    }
)


def _structural_code_index(
    tokens: List[str],
    marker_index: int,
) -> Optional[int]:
    """Nearest numeric reference in safe forward or Russian inverted syntax."""

    candidate_index = marker_index + 1
    while (
        candidate_index < len(tokens)
        and tokens[candidate_index] in _CODE_MARKER_LINK_WORDS
    ):
        candidate_index += 1
    if (
        candidate_index < len(tokens)
        and tokens[candidate_index] != "."
        and _CODE_RE.fullmatch(tokens[candidate_index])
    ):
        return candidate_index

    marker = tokens[marker_index]
    candidate_index = marker_index - 1
    if (
        candidate_index < 0
        or tokens[candidate_index] == "."
        or _CODE_RE.fullmatch(tokens[candidate_index]) is None
    ):
        return None
    if marker in _INVERTED_UNAMBIGUOUS_STRUCTURE_MARKERS:
        return candidate_index
    if (
        marker in _INVERTED_CONTEXTUAL_STRUCTURE_MARKERS
        and candidate_index > 0
        and tokens[candidate_index - 1] in _INVERTED_REFERENCE_PREPOSITIONS
    ):
        return candidate_index
    return None

# Число рядом с такой единицей описывает факты ситуации, а не номер нормы.
# Сравнение по стему покрывает падежи и типичные RP-формулировки.
_SEMANTIC_NUMBER_UNIT_STEMS = (
    "грам", "килограмм", "миллиграм",
    "тонн", "литр", "сантиметр", "миллиметр", "миллилитр",
    "доллар", "рубл",
    "сут", "час", "минут", "секунд",
    "день", "дня", "дней", "недел", "месяц", "год", "лет",
    "процент", "голос",
    "звезд", "уров", "балл",
    "метр", "километр", "миль",
    "штук", "единиц", "патрон", "растен", "семен",
    "человек",
)
_SEMANTIC_NUMBER_EXACT_UNITS = frozenset(
    {
        "раз", "раза", "разов",
        "г", "кг", "мг", "м", "км", "см", "мм",
        "т", "л", "мл", "р", "$", "%",
        "лицо", "лица", "лиц",
        "гражданин", "гражданина", "гражданину", "гражданином",
        "гражданине", "граждане", "граждан", "гражданам",
        "гражданами", "гражданах",
        "гражданка", "гражданки", "гражданке", "гражданку",
        "гражданкой", "гражданок", "гражданкам", "гражданками",
        "гражданках",
    }
)
_SEMANTIC_NUMBER_CUES = frozenset(
    {
        "ровно", "около", "примерно", "почти",
        "до", "от", "свыше", "более", "менее", "выше", "ниже",
        "минимум", "максимум",
        "масса", "массой", "вес", "весом",
        "стоимость", "стоимостью", "сумма", "суммой",
        "штраф", "штрафом", "срок", "сроком",
        "приоритет", "розыск",
    }
)
_PLURAL_STRUCTURE_LIST_MARKERS = frozenset(
    {
        "статьи",
        "статей",
        "статьям",
        "статьями",
        "статьях",
        "главы",
        "глав",
        "главам",
        "главами",
        "главах",
        "разделы",
        "разделов",
        "разделам",
        "разделами",
        "разделах",
        "пункты",
        "пунктов",
        "пунктам",
        "пунктами",
        "пунктах",
        "части",
        "частей",
        "частям",
        "частями",
        "частях",
        "томы",
        "томов",
        "томам",
        "томами",
        "томах",
    }
)
_PLURAL_STRUCTURE_LIST_ABBREVIATIONS = frozenset({"ст"})
_PLURAL_STRUCTURE_LIST_PREFIXES = frozenset({"с", "от"})
_PLURAL_STRUCTURE_LIST_SEPARATORS = frozenset(
    {"и", "или", "либо", "до", "по"}
)
_SEMANTIC_NUMBER_LIST_CONNECTORS = frozenset(
    {"и", "или", "либо", "плюс", "до", "по"}
)
_SEMANTIC_SCALE_WORDS = frozenset(
    {
        "сотня", "сотни", "сотен",
        "тысяч", "тысяча", "тысячи", "тыс",
        "миллион", "миллиона", "миллионов", "млн",
        "миллиард", "миллиарда", "миллиардов", "млрд",
        "к", "k",
    }
)
# Standalone Latin ``k`` is the conventional numeric scale.  Cyrillic ``к``
# is also an ordinary Russian preposition and starts many real article titles;
# treat it as a scale only when a following money/unit token proves that role.
_STANDALONE_SEMANTIC_SCALE_WORDS = (
    _SEMANTIC_SCALE_WORDS - {"к"}
)

_COUNT_UNIT_INVARIANT = frozenset(
    {
        "г", "кг", "мг", "м", "км", "см", "мм",
        "т", "л", "мл", "р", "$", "%",
        "грамм", "килограмм", "миллиграмм",
        "раз", "раза", "разов",
        "сутки", "суток",
        "руб",
    }
)
_COUNT_UNIT_ONE = frozenset(
    {
        "грамм", "килограмм", "миллиграмм",
        "тонна", "литр", "сантиметр", "миллиметр", "миллилитр",
        "доллар", "рубль", "час", "минута", "секунда",
        "день", "неделя", "месяц", "год",
        "процент", "голос", "звезда", "уровень", "балл",
        "метр", "километр", "миля", "штука", "единица",
        "патрон", "растение", "семя", "человек", "лицо",
        "гражданин", "гражданка",
    }
)
_COUNT_UNIT_FEW = frozenset(
    {
        "грамма", "килограмма", "миллиграмма",
        "тонны", "литра", "сантиметра", "миллиметра", "миллилитра",
        "доллара", "рубля", "часа", "минуты", "секунды",
        "дня", "недели", "месяца", "года",
        "процента", "голоса", "звезды", "уровня", "балла",
        "метра", "километра", "мили", "штуки", "единицы",
        "патрона", "растения", "семени", "человека", "лица",
        "гражданина", "гражданки",
    }
)
_COUNT_UNIT_MANY = frozenset(
    {
        "граммов", "килограммов", "миллиграммов",
        "тонн", "литров", "сантиметров", "миллиметров", "миллилитров",
        "долларов", "рублей", "суток", "часов", "минут", "секунд",
        "дней", "недель", "месяцев", "лет",
        "процентов", "голосов", "звезд", "уровней", "баллов",
        "метров", "километров", "миль", "штук", "единиц",
        "патронов", "растений", "семян", "человек", "лиц",
        "граждан", "гражданок",
    }
)


def _is_agreed_count_unit(number_token: str, unit_token: str) -> bool:
    """Recognise a grammatical counted unit, not a same-prefix title word."""

    if unit_token in _COUNT_UNIT_INVARIANT:
        return True
    try:
        value = abs(int(number_token))
    except (TypeError, ValueError):
        return False
    last_two = value % 100
    if 11 <= last_two <= 14:
        category = _COUNT_UNIT_MANY
    else:
        last = value % 10
        if last == 1:
            category = _COUNT_UNIT_ONE
        elif 2 <= last <= 4:
            category = _COUNT_UNIT_FEW
        else:
            category = _COUNT_UNIT_MANY
    return unit_token in category


def _is_semantic_number_unit(token: str) -> bool:
    return (
        token in _SEMANTIC_NUMBER_EXACT_UNITS
        or token.startswith(_SEMANTIC_NUMBER_UNIT_STEMS)
    )


def _shared_semantic_unit_after_number(
    tokens: List[str],
    index: int,
) -> bool:
    """A listed value inherits only a nearby, explicitly typed shared unit."""

    cursor = index + 1
    if (
        cursor >= len(tokens)
        or tokens[cursor] not in _SEMANTIC_NUMBER_LIST_CONNECTORS
    ):
        return False
    cursor += 1
    if (
        cursor >= len(tokens)
        or _CODE_RE.fullmatch(tokens[cursor]) is None
    ):
        return False
    cursor += 1
    if cursor < len(tokens) and tokens[cursor] in _SEMANTIC_SCALE_WORDS:
        cursor += 1
    return (
        cursor < len(tokens)
        and _is_semantic_number_unit(tokens[cursor])
    )


def _compound_scaled_measure_at(tokens: List[str], index: int) -> bool:
    """Recognise `1 million [and] 500 thousand dollars` as one quantity."""

    if (
        index + 1 >= len(tokens)
        or tokens[index + 1] not in _SEMANTIC_SCALE_WORDS
    ):
        return False
    cursor = index + 2
    if (
        cursor < len(tokens)
        and tokens[cursor] in {"и", "плюс"}
    ):
        cursor += 1
    if (
        cursor >= len(tokens)
        or _CODE_RE.fullmatch(tokens[cursor]) is None
    ):
        return False
    cursor += 1
    if (
        cursor >= len(tokens)
        or tokens[cursor] not in _SEMANTIC_SCALE_WORDS
    ):
        return False
    cursor += 1
    return (
        cursor < len(tokens)
        and _is_semantic_number_unit(tokens[cursor])
    )


def _plural_structural_code_indexes(
    tokens: List[str],
    marker_index: int,
) -> Optional[set[int]]:
    """Parse a short plural reference list/range without stealing quantities.

    ``None`` means this is not list syntax and the ordinary single-code parser
    may try. An empty set means list syntax was present but a typed quantity
    proved that it is not a structural code list.
    """

    abbreviated = (
        tokens[marker_index] in _PLURAL_STRUCTURE_LIST_ABBREVIATIONS
    )
    if (
        tokens[marker_index] not in _PLURAL_STRUCTURE_LIST_MARKERS
        and not abbreviated
    ):
        return None
    cursor = marker_index + 1
    while (
        cursor < len(tokens)
        and tokens[cursor] in _CODE_MARKER_LINK_WORDS
    ):
        cursor += 1
    if (
        cursor < len(tokens)
        and tokens[cursor] in _PLURAL_STRUCTURE_LIST_PREFIXES
    ):
        cursor += 1
        while (
            cursor < len(tokens)
            and tokens[cursor] in _CODE_MARKER_LINK_WORDS
        ):
            cursor += 1
    if (
        cursor >= len(tokens)
        or tokens[cursor] == "."
        or _CODE_RE.fullmatch(tokens[cursor]) is None
    ):
        return None

    indexes = {cursor}
    cursor += 1
    while cursor < len(tokens):
        if tokens[cursor] != "." and _CODE_RE.fullmatch(tokens[cursor]):
            indexes.add(cursor)
            cursor += 1
            continue
        if (
            tokens[cursor] in _PLURAL_STRUCTURE_LIST_SEPARATORS
        ):
            next_index = cursor + 1
            while (
                next_index < len(tokens)
                and tokens[next_index] in _CODE_MARKER_LINK_WORDS
            ):
                next_index += 1
            if (
                next_index < len(tokens)
                and tokens[next_index] != "."
                and _CODE_RE.fullmatch(tokens[next_index])
            ):
                indexes.add(next_index)
                cursor = next_index + 1
                continue
        break

    if abbreviated and len(indexes) < 2:
        return None

    for index in indexes:
        if index > 0 and tokens[index - 1] in {"$", "%"}:
            return set()
        if (
            index + 1 < len(tokens)
            and _is_semantic_number_unit(tokens[index + 1])
        ):
            return set()
        if (
            index + 2 < len(tokens)
            and _is_semantic_number_unit(tokens[index + 2])
        ):
            return set()
    return indexes


def _is_semantic_number_context(tokens: List[str], index: int) -> bool:
    """True для количества/денег/срока/процента, а не номера структуры."""

    # Явный «статья/глава/раздел [под номером] N» сильнее слова-единицы:
    # «статья 3 года ...» всё ещё является явным запросом статьи 3.
    for marker_index in range(max(0, index - 3), index):
        if (
            tokens[marker_index] not in _CODE_MARKER_TO_KIND
            and tokens[marker_index] not in _GENERIC_STRUCTURE_CODE_MARKERS
        ):
            continue
        between = tokens[marker_index + 1:index]
        if all(token in _CODE_MARKER_LINK_WORDS for token in between):
            return False

    if index > 0 and tokens[index - 1] in _SEMANTIC_NUMBER_CUES:
        return True
    if (
        index > 0
        and tokens[index - 1] in {"$", "%"}
    ):
        return True
    if (
        index + 1 < len(tokens)
        and tokens[index + 1] in _STANDALONE_SEMANTIC_SCALE_WORDS
    ):
        return True
    if index + 1 < len(tokens) and _is_semantic_number_unit(tokens[index + 1]):
        return True
    if index + 2 < len(tokens) and _is_semantic_number_unit(tokens[index + 2]):
        # «10 тысяч долларов», «5 полных суток».
        return True
    # Несколько чисел перед общей единицей: «от 5 до 20 грамм», «80 + 1 голос».
    if index + 1 < len(tokens) and _CODE_RE.fullmatch(tokens[index + 1]):
        if (
            index + 2 < len(tokens)
            and _is_semantic_number_unit(tokens[index + 2])
        ):
            return True
    if index > 0 and _CODE_RE.fullmatch(tokens[index - 1]):
        if (
            index + 1 < len(tokens)
            and _is_semantic_number_unit(tokens[index + 1])
        ):
            return True
    if _shared_semantic_unit_after_number(tokens, index):
        return True
    if _compound_scaled_measure_at(tokens, index):
        return True
    # A leading number followed by a Russian counted form (plural adjective,
    # genitive singular/plural noun) is a quantity, not shorthand syntax:
    # «5 законных оснований», «2 бандита», «5 положений закона».
    if (
        index + 1 < len(tokens)
        and tokens[index + 1].endswith(
            (
                "ых", "их",
                "ов", "ев", "ей", "ний",
            )
        )
    ):
        return True
    return False


def _definite_leading_count_reason(
    tokens: List[str],
    index: int,
) -> Optional[str]:
    """Explain why a leading literal looks like a quantity."""

    if index + 1 >= len(tokens):
        return None
    next_token = tokens[index + 1]
    if _is_agreed_count_unit(tokens[index], next_token):
        return "typed_unit"
    if (
        index + 2 < len(tokens)
        and (
            tokens[index + 1] in _SEMANTIC_SCALE_WORDS
            or tokens[index + 1] in {'полных', 'полные'}
        )
        and _is_semantic_number_unit(tokens[index + 2])
    ):
        return "typed_scaled_unit"
    if next_token in _STANDALONE_SEMANTIC_SCALE_WORDS:
        return "typed_scale"
    if next_token.endswith(
        ('ых', 'их', 'ов', 'ев', 'ей', 'ний')
    ):
        return "loose_counted_form"
    try:
        value = int(tokens[index])
    except (TypeError, ValueError):
        value = -1
    plural_predicate = any(
        token.endswith(
            (
                'ли', 'ют', 'ут', 'ат', 'ят',
                'ны', 'ены', 'аны', 'яны',
            )
        )
        for token in tokens[index + 2:min(len(tokens), index + 7)]
    )
    if plural_predicate:
        return "loose_plural_predicate"
    # A longer adjective+noun+complement phrase is a strong quantity reading
    # (``2 уставные цели организации``). A short title-shaped phrase such as
    # ``2 общие положения`` remains deliberately dual shorthand.
    if (
        value in {2, 3, 4}
        and next_token.endswith(('ые', 'ие'))
        and index + 3 < len(tokens)
    ):
        return "loose_long_phrase"
    return None


def _is_definite_leading_count(tokens: List[str], index: int) -> bool:
    """Prove a quantity strongly enough to suppress weak article shorthand."""

    return _definite_leading_count_reason(tokens, index) is not None


def _dotted_number_has_typed_value_context(
    tokens: List[str],
    index: int,
) -> bool:
    """Distinguish a decimal measurement from a dotted structural code."""

    token = tokens[index]
    if token.count(".") > 1:
        return False
    if index > 0 and tokens[index - 1] in (
        _SEMANTIC_NUMBER_CUES | {"$", "%"}
    ):
        return True
    if (
        index + 1 < len(tokens)
        and _is_semantic_number_unit(tokens[index + 1])
    ):
        return True
    if (
        index + 2 < len(tokens)
        and (
            tokens[index + 1] in _SEMANTIC_SCALE_WORDS
            or tokens[index + 1] in {"полных", "полные"}
        )
        and _is_semantic_number_unit(tokens[index + 2])
    ):
        return True
    if (
        index + 2 < len(tokens)
        and _CODE_RE.fullmatch(tokens[index + 1])
        and _is_semantic_number_unit(tokens[index + 2])
    ):
        return True
    if (
        index > 0
        and _CODE_RE.fullmatch(tokens[index - 1])
        and index + 1 < len(tokens)
        and _is_semantic_number_unit(tokens[index + 1])
    ):
        return True
    return False


def _semantic_number_is_negated_or_inexact(
    tokens: List[str],
    index: int,
) -> bool:
    prefix = tokens[max(0, index - 2):index]
    if prefix[-1:] == ["не"]:
        return True
    if prefix[-2:] in (
        ["не", "ровно"],
        ["не", "равно"],
        ["точно", "не"],
    ):
        return True
    suffix = tokens[index + 1:min(len(tokens), index + 5)]
    return any(
        left == "не" and right in {"точно", "ровно", "равно"}
        for left, right in zip(suffix, suffix[1:])
    )


def _partition_query_numbers(
    tokens: List[str],
    *,
    allow_leading_dual: bool = False,
    spoken_quantity_indexes: Optional[set[int]] = None,
    semantic_hint_indexes: Optional[set[int]] = None,
    definite_count_indexes: Optional[set[int]] = None,
) -> Tuple[set[int], set[int], set[int]]:
    """Разделить числа на code, semantic и code-only позиции.

    Явный маркер, standalone и безопасный dotted-код — только номер нормы.
    Начальный shorthand без маркера («5 разбой») участвует и как слабый code
    signal, и как смысловой токен. Остальные целые числа остаются частью
    ситуации и не запускают code/doc-scope.
    """

    numeric = [
        index
        for index, token in enumerate(tokens)
        if token != "." and _CODE_RE.fullmatch(token)
    ]
    if not numeric:
        return set(), set(), set()

    spoken_quantity_indexes = spoken_quantity_indexes or set()
    semantic_hint_indexes = semantic_hint_indexes or set()
    definite_count_indexes = definite_count_indexes or set()
    explicit: set[int] = set()
    for marker_index, marker in enumerate(tokens):
        if (
            marker not in _CODE_MARKER_TO_KIND
            and marker not in _GENERIC_STRUCTURE_CODE_MARKERS
        ):
            continue
        plural_indexes = _plural_structural_code_indexes(
            tokens,
            marker_index,
        )
        if plural_indexes is not None:
            explicit.update(plural_indexes)
            continue
        candidate_index = _structural_code_index(tokens, marker_index)
        if (
            candidate_index is not None
            and candidate_index in numeric
            and not (
                candidate_index < marker_index
                and candidate_index
                in (
                    spoken_quantity_indexes
                    | definite_count_indexes
                )
            )
        ):
            explicit.add(candidate_index)

    content_indexes = [
        index
        for index, token in enumerate(tokens)
        if token != "." and token not in _WRAPPER_WORDS
    ]
    standalone = (
        {content_indexes[0]}
        if len(content_indexes) == 1 and content_indexes[0] in numeric
        else set()
    )

    code_only = set(explicit) | set(standalone)
    dotted = {
        index
        for index in numeric
        if "." in tokens[index]
        and not _dotted_number_has_typed_value_context(tokens, index)
    }
    code_only.update(dotted)
    semantic = {
        index
        for index in numeric
        if index not in code_only
        and (
            index in semantic_hint_indexes
            or _is_semantic_number_context(tokens, index)
        )
    }
    document_qualified = {
        index
        for index in _document_qualified_number_indexes(tokens)
        if index not in semantic
    }
    code_only.update(document_qualified)
    # Неформальный «5 разбой» сохраняем как dual signal. Если после числа идёт
    # единица («3 раза», «25 грамм»), semantic-context выше намеренно запрещает code.
    dual: set[int] = set()
    if content_indexes:
        first = content_indexes[0]
        if (
            allow_leading_dual
            and
            first in numeric
            and first not in code_only
            and first not in definite_count_indexes
            and not _is_definite_leading_count(tokens, first)
            and not (
                "." in tokens[first]
                and _dotted_number_has_typed_value_context(tokens, first)
            )
        ):
            dual.add(first)

    semantic.update(index for index in numeric if index not in code_only)
    code = code_only | dual
    return code, semantic, code_only


def _query_marker_tokens(nq: NormalizedQuery) -> List[str]:
    tokens = _TOKEN_RE.findall(
        nq.original.lower().replace("ё", "е")
    )
    tokens = [
        "." if token in _DOT_WORDS else token.strip(".") or "."
        for token in tokens
    ]
    return _words_to_digits([token for token in tokens if token])


def _requested_code_kind(nq: NormalizedQuery) -> Optional[str]:
    """Return one unambiguous structural kind explicitly named by the user."""

    if not nq.codes:
        return None
    tokens = _query_marker_tokens(nq)
    mentioned = {
        kind
        for token in tokens
        if (kind := _CODE_MARKER_TO_KIND.get(token)) is not None
    }
    requested: set[str] = set()
    for index, token in enumerate(tokens):
        kind = _CODE_MARKER_TO_KIND.get(token)
        if kind is None:
            continue
        plural_indexes = _plural_structural_code_indexes(tokens, index)
        if plural_indexes:
            requested.add(kind)
            continue
        if plural_indexes is None:
            code_index = _structural_code_index(tokens, index)
            if code_index is not None:
                requested.add(kind)
    if len(requested) != 1 or mentioned != requested:
        return None
    return next(iter(requested))


def _document_scope_qualifier(
    nq: NormalizedQuery,
    requested_kind: Optional[str],
) -> str:
    """Remove the explicit ``kind [под номером] N`` syntax from a doc qualifier."""

    tokens = _query_marker_tokens(nq)
    removed: set[int] = set()
    for marker_index, marker in enumerate(tokens):
        marker_kind = _CODE_MARKER_TO_KIND.get(marker)
        is_requested_marker = (
            requested_kind is not None
            and marker_kind == requested_kind
        )
        is_generic_marker = (
            requested_kind is None
            and marker in _GENERIC_STRUCTURE_CODE_MARKERS
        )
        if not is_requested_marker and not is_generic_marker:
            continue

        plural_indexes = _plural_structural_code_indexes(
            tokens,
            marker_index,
        )
        if plural_indexes:
            removed.add(marker_index)
            removed.update(plural_indexes)
            last_code = max(plural_indexes)
            removed.update(
                index
                for index in range(marker_index + 1, last_code)
                if tokens[index] in (
                    _PLURAL_STRUCTURE_LIST_PREFIXES
                    | _PLURAL_STRUCTURE_LIST_SEPARATORS
                    | _CODE_MARKER_LINK_WORDS
                )
            )
            continue

        code_index = _structural_code_index(tokens, marker_index)
        if code_index is None:
            continue
        removed.update({marker_index, code_index})
        left, right = sorted((marker_index, code_index))
        removed.update(
            index
            for index in range(left + 1, right)
            if tokens[index] in _CODE_MARKER_LINK_WORDS
        )
        if (
            code_index < marker_index
            and code_index > 0
            and tokens[code_index - 1] in _INVERTED_REFERENCE_PREPOSITIONS
        ):
            removed.add(code_index - 1)

    for index, token in enumerate(tokens):
        if _CODE_RE.fullmatch(token) and token in nq.codes:
            removed.add(index)
            if (
                index > 0
                and tokens[index - 1] in _INVERTED_REFERENCE_PREPOSITIONS
            ):
                removed.add(index - 1)

    return " ".join(
        token
        for index, token in enumerate(tokens)
        if index not in removed
        and token != "."
        and token not in _WRAPPER_WORDS
    ).strip()


def _query_marker_tokens_with_spans(
    nq: NormalizedQuery,
) -> Tuple[List[str], List[Tuple[int, int]]]:
    """Return marker tokens plus their exact spans in the untouched query."""

    lowered = nq.original.lower().replace("ё", "е")
    matches = list(_TOKEN_RE.finditer(lowered))
    raw_tokens = [
        "." if match.group(0) in _DOT_WORDS
        else match.group(0).strip(".") or "."
        for match in matches
    ]
    tokens, surfaces = _words_to_digits_with_surfaces(raw_tokens)
    spans: List[Tuple[int, int]] = []
    cursor = 0
    for surface_group in surfaces:
        consumed = len(surface_group)
        if consumed <= 0 or cursor + consumed > len(matches):
            return [], []
        spans.append(
            (
                matches[cursor].start(),
                matches[cursor + consumed - 1].end(),
            )
        )
        cursor += consumed
    if cursor != len(matches) or len(tokens) != len(spans):
        return [], []
    return tokens, spans


def _explicit_code_title_candidate(
    nq: NormalizedQuery,
) -> Optional[Tuple[str, str, str]]:
    """Bind one leading explicit structure reference to its untouched title.

    Only the command-like prefix is parsed.  The raw suffix is never
    token-normalised, so a real title may contain numeral words, dates,
    additional codes, or words such as ``глава`` without changing the bound
    leading reference.
    """

    tokens, spans = _query_marker_tokens_with_spans(nq)
    if not tokens:
        return None
    candidates: List[Tuple[int, int, str]] = []
    prefix_fillers = (
        _WRAPPER_WORDS
        | _INVERTED_REFERENCE_PREPOSITIONS
        | {"."}
    )
    for marker_index, marker in enumerate(tokens):
        requested_kind = _CODE_MARKER_TO_KIND.get(marker)
        if requested_kind is None:
            continue
        plural_indexes = _plural_structural_code_indexes(
            tokens,
            marker_index,
        )
        if plural_indexes:
            if len(plural_indexes) != 1:
                continue
            code_index = next(iter(plural_indexes))
        else:
            code_index = _structural_code_index(tokens, marker_index)
        if code_index is None:
            continue
        prefix_end = min(marker_index, code_index)
        if any(
            token not in prefix_fillers
            for token in tokens[:prefix_end]
        ):
            continue
        candidates.append(
            (marker_index, code_index, requested_kind)
        )
        # Any marker/code inside the title is intentionally ignored: it lies
        # after the already bound leading reference and belongs to the suffix.
        break
    if len(candidates) != 1:
        return None
    marker_index, code_index, requested_kind = candidates[0]
    suffix_start = spans[max(marker_index, code_index)][1]
    title_remainder = nq.original[suffix_start:].strip()
    if not title_remainder or _TOKEN_RE.search(title_remainder) is None:
        return None
    return tokens[code_index], title_remainder, requested_kind


def _code_signal_reason(nq: NormalizedQuery) -> Optional[str]:
    """Classify code context for telemetry and narrow kind filtering."""
    if not nq.codes:
        return None
    tokens = _query_marker_tokens(nq)
    requested_kind = _requested_code_kind(nq)
    if requested_kind is not None:
        return f"explicit_{requested_kind}_marker"
    if any(
        token in _GENERIC_STRUCTURE_CODE_MARKERS
        and _structural_code_index(tokens, index) is not None
        for index, token in enumerate(tokens)
    ):
        return "explicit_structure_marker"
    if _document_qualified_number_indexes(tokens):
        return "document_qualified_number"
    if len(tokens) == 1:
        return "standalone_number"
    if any("." in code for code in nq.codes):
        return "dotted_number"
    return "embedded_integer"


# --- LRU-кэш эмбеддингов запросов ---------------------------------------------------------------

_EMBED_CACHE: "OrderedDict[str, List[float]]" = OrderedDict()
_EMBED_CACHE_MAX = 256

# asyncio-семафор привязан к event loop. В production loop один, а WeakKeyDictionary сохраняет
# корректность небольших async-тестов, которые создают новый loop на каждый сценарий.
_FRIDA_SLOTS: "weakref.WeakKeyDictionary[Any, asyncio.BoundedSemaphore]" = weakref.WeakKeyDictionary()

LegStatus = Literal["ok", "empty", "skipped", "timeout", "error"]


class LawSearchUnavailable(RuntimeError):
    """Критическая фаза не дала карточки до дедлайна; HTTP-слой вернёт 503."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass
class _LegOutcome:
    """Внутренний честный результат одной ноги; наружу уходят mode/warnings, а не эта структура."""
    status: LegStatus
    ids: List[int] = field(default_factory=list)
    # Абсолютная уверенность нужна прежде всего fuzzy-ногам. Без неё RRF превращает
    # similarity=0.34 в такой же сильный rank-1, как настоящее совпадение.
    confidences: Dict[int, float] = field(default_factory=dict)
    # Raw scores are telemetry-only.  They are never consumed by _fuse; this
    # lets us evaluate future calibration offline without changing live order.
    raw_scores: Dict[int, float] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    degraded: bool = False
    usable: bool = False              # для vector: участвовала ли совместимая FRIDA-нога
    elapsed_ms: Optional[float] = None


async def _timed_outcome(awaitable: Awaitable[_LegOutcome]) -> _LegOutcome:
    """Measure a leg without changing its timeout, result or error semantics."""
    loop = asyncio.get_running_loop()
    started = loop.time()
    outcome = await awaitable
    outcome.elapsed_ms = round((loop.time() - started) * 1000.0, 3)
    return outcome


def _leg_diagnostics(outcome: _LegOutcome) -> Dict[str, Any]:
    """Compact internal trace used only by the quality log."""
    return {
        "status": outcome.status,
        "count": len(outcome.ids),
        "ids": list(outcome.ids[:20]),
        "confidences": {
            str(article_id): round(float(confidence), 6)
            for article_id, confidence in list(outcome.confidences.items())[:20]
        },
        "raw_scores": {
            str(article_id): round(float(score), 6)
            for article_id, score in list(outcome.raw_scores.items())[:20]
        },
        "warnings": list(outcome.warnings),
        "degraded": outcome.degraded,
        "usable": outcome.usable,
        "elapsed_ms": outcome.elapsed_ms,
    }


@dataclass(frozen=True)
class _VectorCorpusState:
    total: int
    ready: int
    queued: int
    failed: int
    mismatched: int
    passport_found: bool
    model_name: Optional[str]
    model_revision: Optional[str]
    dimensions: Optional[int]
    query_prefix: Optional[str]
    document_prefix: Optional[str]
    pooling: Optional[str]
    normalized: Optional[bool]
    recipe_version: Optional[str]
    runtime_compatible: bool

    @property
    def complete(self) -> bool:
        return (
            self.ready == self.total
            and self.queued == 0
            and self.failed == 0
            and self.mismatched == 0
        )

    @property
    def compatible(self) -> bool:
        return self.passport_found and (
            self.model_name == config.LAW_CATALOG_EMBED_MODEL_NAME
            and self.model_revision == config.LAW_CATALOG_EMBED_MODEL_REVISION
            and self.dimensions == config.LAW_CATALOG_EMBED_DIMS
            and self.query_prefix == config.LAW_CATALOG_EMBED_QUERY_PREFIX
            and self.document_prefix == config.LAW_CATALOG_EMBED_DOC_PREFIX
            and self.pooling == config.LAW_CATALOG_EMBED_POOLING
            and self.normalized == config.LAW_CATALOG_EMBED_NORMALIZED
            and self.recipe_version == config.LAW_CATALOG_EMBED_RECIPE_VERSION
            and self.runtime_compatible
        )


def _cache_get(key: str) -> Optional[List[float]]:
    vec = _EMBED_CACHE.get(key)
    if vec is not None:
        _EMBED_CACHE.move_to_end(key)
    return vec


def _cache_put(key: str, vec: List[float]) -> None:
    _EMBED_CACHE[key] = vec
    _EMBED_CACHE.move_to_end(key)
    while len(_EMBED_CACHE) > _EMBED_CACHE_MAX:
        _EMBED_CACHE.popitem(last=False)


def _frida_slots() -> asyncio.BoundedSemaphore:
    loop = asyncio.get_running_loop()
    slots = _FRIDA_SLOTS.get(loop)
    if slots is None:
        slots = asyncio.BoundedSemaphore(config.LAW_SEARCH_EMBED_MAX_INFLIGHT)
        _FRIDA_SLOTS[loop] = slots
    return slots


def _to_pgvector(values: List[float]) -> str:
    """Локальное текстовое представление параметра pgvector."""
    return "[" + ",".join(repr(float(v)) for v in values) + "]"


async def _db_fetch(
    query: str,
    *args: Any,
    timeout: float,
    local_settings: Optional[Dict[str, str]] = None,
) -> Any:
    """asyncpg fetch с серверным statement_timeout, scoped только этой транзакцией."""
    timeout_ms = max(1, int(timeout * 1000))
    async with db_manager.transaction() as conn:
        await conn.execute("SELECT set_config('statement_timeout', $1, true)", f"{timeout_ms}ms")
        for name, value in (local_settings or {}).items():
            await conn.execute("SELECT set_config($1, $2, true)", name, value)
        try:
            return await conn.fetch(query, *args)
        except Exception as exc:
            if getattr(exc, "sqlstate", None) == "57014":  # query_canceled / statement_timeout
                raise asyncio.TimeoutError from exc
            raise


async def _db_fetchrow(query: str, *args: Any, timeout: float) -> Any:
    """asyncpg fetchrow с тем же scoped statement_timeout."""
    timeout_ms = max(1, int(timeout * 1000))
    async with db_manager.transaction() as conn:
        await conn.execute("SELECT set_config('statement_timeout', $1, true)", f"{timeout_ms}ms")
        try:
            return await conn.fetchrow(query, *args)
        except Exception as exc:
            if getattr(exc, "sqlstate", None) == "57014":
                raise asyncio.TimeoutError from exc
            raise


# --- Квалификатор документа («статья N <документ>») ---------------------------------------------
# Аббревиатуры кодексов → стем для матча по doc_title. Полные слова («уголовный», «конституция»,
# «дорожный») стемятся автоматически в _doc_keywords и в карте не нужны — тут ТОЛЬКО сокращения,
# которые из слова не вывести. Стандартные юр-названия есть в каталоге КАЖДОГО сервера → обобщается.
_DOC_ALIASES = {
    'ук': 'уголовн', 'уголовка': 'уголовн',
    'упк': 'процессуальн',
    'коап': 'администрат', 'коа': 'администрат', 'админ': 'администрат',
    'пдд': 'дорожн',
    'гк': 'граждан',
    'тк': 'трудов',
    'нк': 'налог',
    'ск': 'семейн',
}
# Conservative second proof for inflected full titles whose simple suffix stem
# is imperfect. Production contrasts: «административного кодекса»→its document
# 0.667, while wanted/vehicle concept-only incidental title hits are 0.094/0.129.
_DOC_SCOPE_TITLE_SIMILARITY = 0.35
_DOCUMENT_TYPE_FORMS = frozenset(
    {
        "кодекс", "кодекса", "кодексу", "кодексе", "кодексом",
        "кодексы", "кодексов", "кодексам", "кодексами", "кодексах",
        "конституция", "конституции", "конституцию", "конституцией",
        "конституций", "конституциям", "конституциями", "конституциях",
        "закон", "закона", "закону", "законе", "законом",
        "законы", "законов", "законам", "законами", "законах",
        "устав", "устава", "уставу", "уставе", "уставом",
        "уставы", "уставов", "уставам", "уставами", "уставах",
        "регламент", "регламента", "регламенту", "регламенте",
        "регламентом", "регламенты", "регламентов", "регламентам",
        "регламентами", "регламентах",
    }
)


def _document_qualified_number_indexes(tokens: List[str]) -> set[int]:
    """Numbers directly attached to an explicit law-document name/type."""

    def is_document_token(token: str) -> bool:
        return (
            token in _DOC_ALIASES
            or token in _DOCUMENT_TYPE_FORMS
        )

    def is_singular_document_adjective(token: str) -> bool:
        return token.endswith(
            (
                "ский", "ская", "ское", "ского", "скому", "ским",
                "ском", "скую",
                "ный", "ная", "ное", "ного", "ному", "ным",
                "ном", "ную",
                "овой", "овая", "овое", "ового", "овому", "овым",
                "овом", "овую",
            )
        )

    indexes: set[int] = set()
    for index, token in enumerate(tokens):
        if token == "." or _CODE_RE.fullmatch(token) is None:
            continue

        left = index - 1
        while left >= 0 and tokens[left] in _CODE_MARKER_LINK_WORDS:
            left -= 1
        if left >= 0 and is_document_token(tokens[left]):
            indexes.add(index)
            continue

        right = index + 1
        if right < len(tokens) and is_document_token(tokens[right]):
            indexes.add(index)
            continue
        # «в 5 гражданском кодексе»: one descriptive adjective may stand
        # between the number and the exact document-type noun.
        if (
            right + 1 < len(tokens)
            and is_singular_document_adjective(tokens[right])
            and is_document_token(tokens[right + 1])
        ):
            indexes.add(index)
    return indexes


def _has_explicit_document_signal(nq: NormalizedQuery) -> bool:
    """Есть ли в запросе именно название/тип НПА, а не случайная тема документа."""

    tokens = _query_marker_tokens(nq)
    return any(
        token in _DOC_ALIASES
        or token in _DOCUMENT_TYPE_FORMS
        for token in tokens
    )


def _should_resolve_document_scope(
    nq: NormalizedQuery,
    requested_kind: Optional[str],
) -> bool:
    """Разрешить ранний code+document path только при авторитетном синтаксисе."""

    if not nq.codes or not nq.cleaned:
        return False
    if requested_kind is not None:
        return True
    # Shorthand без «статья» допустим для буквальной начальной цифры или
    # числа, непосредственно привязанного к типу/алиасу документа:
    # «1 административный кодекс», «12.5 УК», «УК 5».
    # «одна партия ... конгресса» и любые количества сюда не попадают.
    marker_tokens = _query_marker_tokens(nq)
    return (
        bool(_document_qualified_number_indexes(marker_tokens))
        and _has_explicit_document_signal(nq)
    )


def _doc_keywords(qualifier: str) -> List[str]:
    """Слова квалификатора → подстроки для матча по doc_title (аббревиатуры развёрнуты по карте,
    длинные слова подрезаны до стема, чтобы падежи/род не мешали: «конституция»/«конституции» →
    «конституц»). Слова < 4 символов и не-аббревиатуры отбрасываем как шум."""
    out: List[str] = []
    for w in qualifier.split():
        w = w.strip()
        if not w:
            continue
        if w in _DOC_ALIASES:
            out.append(_DOC_ALIASES[w])
        elif len(w) >= 7 and w.endswith(("ого", "его", "ому", "ему")):
            # Прилагательные в родительном/дательном падеже: простое ``[:-2]``
            # оставляет ложный хвост «уголовно» и ставит
            # «уголовно-процессуальный» выше основного «уголовный».
            out.append(w[:-3])
        elif len(w) >= 7:
            out.append(w[:-2])
        elif len(w) >= 5:
            out.append(w[:-1])
        elif len(w) >= 4:
            out.append(w)
    # уникализируем, сохраняя порядок
    seen: set = set()
    return [k for k in out if not (k in seen or seen.add(k))]


# --- Ноги поиска (каждая возвращает [(article_id, rank_position), ...]) -------------------------

async def _leg_code_query(
    server_id: str,
    codes: List[str],
    *,
    requested_kind: Optional[str] = None,
) -> List[int]:
    rows = await _db_fetch(
        """
        SELECT id FROM law_articles
        WHERE server_id = $1 AND (code = ANY($2) OR code LIKE ANY($3))
          AND ($4::text IS NULL OR kind = $4)
        ORDER BY
          (code = ANY($2)) DESC,
          code,
          law_document_id,
          source_order,
          id
        LIMIT 20
        """,
        server_id,
        codes,
        [c + '.%' for c in codes],
        requested_kind,
        timeout=config.LAW_SEARCH_SQL_TIMEOUT,
    )
    return [r["id"] for r in rows]


async def _leg_code(server_id: str, codes: List[str],
                    fallback_codes: Optional[List[str]] = None,
                    *, requested_kind: Optional[str] = None) -> List[int]:
    if not codes:
        return []
    ids = await _leg_code_query(
        server_id,
        codes,
        requested_kind=requested_kind,
    )
    # Дефис-склейка («статьи 10-15»→«10.15») не нашлась в каталоге → это был диапазон,
    # а не поднумерация «10-1»: ищем по частям (прежнее поведение до канонизации).
    if not ids and fallback_codes:
        ids = await _leg_code_query(
            server_id,
            fallback_codes,
            requested_kind=requested_kind,
        )
    return ids


async def _leg_code_title_match(
    server_id: str,
    code: str,
    title_remainder: str,
    requested_kind: Optional[str],
) -> List[int]:
    """Match ``code + complete title`` inside one server and optional kind.

    The query is intentionally stricter than ordinary title retrieval: same
    server, exact code, and either normalized title equality or an identical
    Russian stem signature with at least two lexemes.  Callers decide whether
    the evidence is only a weak shorthand rescue or an explicit structural
    reference.
    """

    if not code or not title_remainder:
        return []
    rows = await _db_fetch(
        """
        WITH query_data AS MATERIALIZED (
          SELECT
            trim(
              regexp_replace(
                lower(replace($3::text, 'ё', 'е')),
                '[^[:alnum:]]+',
                ' ',
                'g'
              )
            ) AS normalized_title,
            strip(to_tsvector('russian', $3::text)) AS title_signature
        )
        SELECT article.id
        FROM law_articles article
        CROSS JOIN query_data
        WHERE article.server_id = $1
          AND article.code = $2
          AND ($4::text IS NULL OR article.kind = $4)
          AND article.title IS NOT NULL
          AND (
            trim(
              regexp_replace(
                lower(replace(article.title, 'ё', 'е')),
                '[^[:alnum:]]+',
                ' ',
                'g'
              )
            ) = query_data.normalized_title
            OR (
              cardinality(
                tsvector_to_array(query_data.title_signature)
              ) >= 2
              AND strip(
                to_tsvector('russian', article.title)
              ) = query_data.title_signature
            )
          )
        ORDER BY article.law_document_id, article.source_order, article.id
        LIMIT 20
        """,
        server_id,
        code,
        title_remainder,
        requested_kind,
        timeout=config.LAW_SEARCH_SQL_TIMEOUT,
    )
    return [int(row["id"]) for row in rows]


async def _leg_code_title_rescue(
    server_id: str,
    code: str,
    title_remainder: str,
) -> List[int]:
    """Weakly rescue an implicit ``code + exact title`` shorthand."""

    return await _leg_code_title_match(
        server_id,
        code,
        title_remainder,
        None,
    )


async def _leg_explicit_code_title(
    server_id: str,
    code: str,
    title_remainder: str,
    requested_kind: str,
) -> List[int]:
    """Resolve an explicit ``article/chapter/section N + exact title``."""

    return await _leg_code_title_match(
        server_id,
        code,
        title_remainder,
        requested_kind,
    )


async def _leg_document_scope(
    server_id: str,
    qualifier: str,
    keywords: List[str],
) -> List[int]:
    """Resolve the best matching document(s) before looking at code or kind.

    The qualifier is locked to the document(s) with the best keyword coverage
    and title similarity.  This deliberately preserves the old tie-break for
    short aliases too: in production ``ук`` ranks the main Criminal Code above
    appendices that merely contain the same stem.
    """
    if not keywords:
        return []
    like_kw = ["%" + keyword + "%" for keyword in keywords]
    rows = await _db_fetch(
        """
        WITH scored AS (
          SELECT
            id AS law_document_id,
            (SELECT count(*) FROM unnest($2::text[]) keyword
               WHERE lower(title) LIKE '%' || keyword || '%') AS keyword_hits,
            similarity(lower(title), $3) AS title_similarity
          FROM law_documents
          WHERE server_id = $1
            AND coalesce(title, '') <> ''
            AND lower(title) LIKE ANY($4)
        ),
        ranked AS (
          SELECT
            law_document_id,
            dense_rank() OVER (
              ORDER BY
                keyword_hits DESC,
                title_similarity DESC
            ) AS scope_rank
          FROM scored
        )
        SELECT law_document_id AS id
        FROM ranked
        WHERE scope_rank = 1
        ORDER BY law_document_id
        LIMIT 20
        """,
        server_id,
        keywords,
        qualifier,
        like_kw,
        timeout=config.LAW_SEARCH_SQL_TIMEOUT,
    )
    return [row["id"] for row in rows]


async def _leg_document_scope_authoritative(
    server_id: str,
    document_ids: List[int],
    qualifier: str,
    keywords: List[str],
) -> List[int]:
    """Keep only document-like scopes, rejecting incidental concept matches."""

    if not document_ids or not keywords:
        return []
    rows = await _db_fetch(
        """
        SELECT id
        FROM law_documents
        WHERE server_id = $1
          AND id = ANY($2::int[])
          AND (
            NOT EXISTS (
              SELECT 1
              FROM unnest($3::text[]) keyword
              WHERE lower(coalesce(title, '')) NOT LIKE '%' || keyword || '%'
            )
            OR similarity(lower(coalesce(title, '')), $4) >= $5
          )
        ORDER BY id
        LIMIT 20
        """,
        server_id,
        document_ids,
        keywords,
        qualifier,
        _DOC_SCOPE_TITLE_SIMILARITY,
        timeout=config.LAW_SEARCH_SQL_TIMEOUT,
    )
    return [row["id"] for row in rows]


async def _leg_code_doc_query(
    server_id: str,
    codes: List[str],
    document_ids: List[int],
    *,
    requested_kind: Optional[str] = None,
) -> List[int]:
    rows = await _db_fetch(
        """
        SELECT id
        FROM law_articles
        WHERE server_id = $1 AND (code = ANY($2) OR code LIKE ANY($3))
          AND law_document_id = ANY($4::int[])
          AND ($5::text IS NULL OR kind = $5)
        ORDER BY
          (code = ANY($2)) DESC,
          code,
          law_document_id,
          source_order,
          id
        LIMIT 20
        """,
        server_id,
        codes,
        [c + '.%' for c in codes],
        document_ids,
        requested_kind,
        timeout=config.LAW_SEARCH_SQL_TIMEOUT,
    )
    return [r["id"] for r in rows]


async def _leg_code_doc(server_id: str, codes: List[str], qualifier: str,
                        keywords: List[str],
                        fallback_codes: Optional[List[str]] = None,
                        *, requested_kind: Optional[str] = None,
                        document_ids: Optional[List[int]] = None) -> List[int]:
    """Целевой «<тип> N <документ>» внутри заранее разрешённого document scope.

    Явный непротиворечивый тип «статья»/«глава»/«раздел» фильтруется до LIMIT;
    точный код идёт выше префиксного. Пустой результат означает, что распознанный
    документ не содержит такого code/kind, и вызывающий код не должен переходить
    к другим документам. fallback_codes — части дефис-склейки (см. _leg_code).
    qualifier/keywords оставлены в сигнатуре, чтобы эта внутренняя нога явно
    сохраняла исходный контекст запроса; document_ids являются авторитетной областью."""
    if not codes or not keywords or not document_ids:
        return []
    ids = await _leg_code_doc_query(
        server_id,
        codes,
        document_ids,
        requested_kind=requested_kind,
    )
    if not ids and fallback_codes:
        ids = await _leg_code_doc_query(
            server_id,
            fallback_codes,
            document_ids,
            requested_kind=requested_kind,
        )
    return ids


async def _leg_synonyms(server_id: str, cleaned: str) -> List[Tuple[int, float]]:
    if len(cleaned) < 2:
        return []
    rows = await _db_fetch(
        """
        SELECT article_id,
               LEAST(1.0, GREATEST(0.0, MAX(similarity(term, $2) * weight))) AS score
        FROM law_synonyms
        WHERE server_id = $1 AND status = 'approved'
          AND (term % $2 OR position(term in $2) > 0)
        GROUP BY article_id
        ORDER BY score DESC, article_id
        LIMIT 15
        """,
        server_id, cleaned, timeout=config.LAW_SEARCH_SQL_TIMEOUT,
    )
    return [(r["article_id"], float(r["score"] or 0.0)) for r in rows]


async def _leg_synonym_exact(server_id: str, cleaned: str) -> List[int]:
    """Высокоточная нога: игрок сказал словарную фразу ЦЕЛИКОМ.

    Сравнивается полный набор нормализованных russian-лексем, а не подстрока. Иначе
    общий auto-term «розыск» внутри длинного ситуационного запроса получал exact-вес
    и заодно отключал vector boost.

    Прямое попадание в словарь синонимов — сильный сигнал (LLM/юзер прямо указали: этой
    формулировкой ищут именно эту статью), в отличие от рыхлого триграммного совпадения в
    обычной synonym-ноге. Вес высокий, как у кода/названия."""
    if len(cleaned) < 4:
        return []
    rows = await _db_fetch(
        """
        SELECT article_id
        FROM law_synonyms
        WHERE server_id = $1 AND status = 'approved' AND char_length(term) >= 4
          AND cardinality(tsvector_to_array(to_tsvector('russian', term))) > 0
          AND tsvector_to_array(to_tsvector('russian', term))
              = tsvector_to_array(to_tsvector('russian', $2))
          -- Russian FTS удаляет «без/не/с» как стоп-слова. Сверяем их отдельно,
          -- чтобы противоположные «с целью» и «без цели» не считались exact-hit.
          AND (to_tsvector('simple', term) @@ plainto_tsquery('simple', 'без'))
              = (to_tsvector('simple', $2) @@ plainto_tsquery('simple', 'без'))
          AND (to_tsvector('simple', term) @@ plainto_tsquery('simple', 'не'))
              = (to_tsvector('simple', $2) @@ plainto_tsquery('simple', 'не'))
          AND (to_tsvector('simple', term) @@ phraseto_tsquery('simple', 'без цели'))
              = (to_tsvector('simple', $2) @@ phraseto_tsquery('simple', 'без цели'))
          AND (to_tsvector('simple', term) @@ phraseto_tsquery('simple', 'с целью'))
              = (to_tsvector('simple', $2) @@ phraseto_tsquery('simple', 'с целью'))
        GROUP BY article_id
        ORDER BY MAX(weight) DESC, article_id
        LIMIT 10
        """,
        server_id, cleaned, timeout=config.LAW_SEARCH_SQL_TIMEOUT,
    )
    return [r["article_id"] for r in rows]


async def _leg_fts(
    server_id: str,
    cleaned: str,
    tokens: List[str],
) -> List[Tuple[int, float]]:
    if not cleaned:
        return []
    has_without, has_not, has_without_purpose, has_with_purpose = _polarity_flags(cleaned)
    # Строгий AND (websearch) — точные запросы; relaxed OR — добор при шумном голосе.
    # ts_rank_cd norm 33 = 1|32: (1) ранг делится на 1+log(длины) — глушит статьи-простыни
    # («Основные термины…» матчится на всё), (32) нормировка rank/(rank+1) в 0..1.
    rows = await _db_fetch(
        """
        SELECT a.id, ts_rank_cd(a.fts, q, 33) AS rank
        FROM law_articles a, websearch_to_tsquery('russian', $2) q
        WHERE a.server_id = $1 AND a.fts @@ q
          AND (NOT $3::boolean OR to_tsvector('simple', coalesce(a.full_text, ''))
              @@ plainto_tsquery('simple', 'без'))
          AND (NOT $4::boolean OR to_tsvector('simple', coalesce(a.full_text, ''))
              @@ plainto_tsquery('simple', 'не'))
          AND (NOT $5::boolean OR to_tsvector('simple', coalesce(a.full_text, ''))
              @@ phraseto_tsquery('simple', 'без цели'))
          AND (NOT $6::boolean OR to_tsvector('simple', coalesce(a.full_text, ''))
              @@ phraseto_tsquery('simple', 'с целью'))
        ORDER BY rank DESC
        LIMIT 15
        """,
        server_id, cleaned, has_without, has_not, has_without_purpose, has_with_purpose,
        timeout=config.LAW_SEARCH_SQL_TIMEOUT,
    )
    hits: List[Tuple[int, float]] = [(r["id"], 1.0) for r in rows]
    if len(hits) < 5 and len(tokens) > 1:
        safe = [re.sub(r'[^\wа-яё]', '', t) for t in tokens]
        or_query = ' | '.join(t for t in safe if t)
        if or_query:
            # Ошибка второго SQL-вызова — реальный отказ FTS-ноги, а не валидная
            # пустая выдача. Токены выше очищены, поэтому не маскируем connection/
            # statement errors под якобы безобидную ошибку синтаксиса tsquery.
            extra = await _db_fetch(
                """
                WITH query_terms AS (
                  SELECT tsvector_to_array(to_tsvector('russian', $7)) AS terms
                )
                SELECT a.id, ts_rank_cd(a.fts, q, 33) AS rank,
                       LEAST(1.0, GREATEST(
                         0.0,
                         (
                           SELECT count(*)::float
                           FROM unnest(query_terms.terms) AS source(term)
                           WHERE source.term = ANY(tsvector_to_array(a.fts))
                         ) / NULLIF(cardinality(query_terms.terms), 0)
                       )) AS confidence
                FROM law_articles a, to_tsquery('russian', $2) q, query_terms
                WHERE a.server_id = $1 AND a.fts @@ q
                  AND (NOT $3::boolean OR to_tsvector('simple', coalesce(a.full_text, ''))
                      @@ plainto_tsquery('simple', 'без'))
                  AND (NOT $4::boolean OR to_tsvector('simple', coalesce(a.full_text, ''))
                      @@ plainto_tsquery('simple', 'не'))
                  AND (NOT $5::boolean OR to_tsvector('simple', coalesce(a.full_text, ''))
                      @@ phraseto_tsquery('simple', 'без цели'))
                  AND (NOT $6::boolean OR to_tsvector('simple', coalesce(a.full_text, ''))
                      @@ phraseto_tsquery('simple', 'с целью'))
                ORDER BY confidence DESC, rank DESC, a.id
                LIMIT 15
                """,
                server_id, or_query, has_without, has_not,
                has_without_purpose, has_with_purpose, cleaned,
                timeout=config.LAW_SEARCH_SQL_TIMEOUT,
            )
            seen = {article_id for article_id, _confidence in hits}
            hits.extend(
                (r["id"], float(r["confidence"] or 0.0))
                for r in extra
                if r["id"] not in seen
            )
    return hits


async def _leg_title(server_id: str, cleaned: str) -> List[int]:
    """Высокоточная нога: лексемы запроса стоят в НАЗВАНИИ статьи.

    Когда юзер называет сам состав («халатность», «разбой»), нужная статья названа
    так же — а шумные авто-синонимы на соседних статьях («халатность с медкартой»)
    в чужие названия не попадают. websearch = AND всех лексем (морфология russian);
    ts_rank_cd norm 1 (делёж на 1+log длины) поднимает статьи, где запрос — почти всё
    название («Халатность» выше «Халатность при исполнении должностных обязанностей»).
    Вес леги высокий (как код) — точное совпадение с именем статьи авторитетно.
    """
    if len(cleaned) < 2:
        return []
    has_without, has_not, has_without_purpose, has_with_purpose = _polarity_flags(cleaned)
    rows = await _db_fetch(
        """
        SELECT a.id, ts_rank_cd(to_tsvector('russian', coalesce(a.title, '')), q, 1) AS rank
        FROM law_articles a, websearch_to_tsquery('russian', $2) q
        WHERE a.server_id = $1 AND a.title IS NOT NULL
          AND to_tsvector('russian', coalesce(a.title, '')) @@ q
          AND (NOT $3::boolean OR to_tsvector('simple', a.title)
              @@ plainto_tsquery('simple', 'без'))
          AND (NOT $4::boolean OR to_tsvector('simple', a.title)
              @@ plainto_tsquery('simple', 'не'))
          AND (NOT $5::boolean OR to_tsvector('simple', a.title)
              @@ phraseto_tsquery('simple', 'без цели'))
          AND (NOT $6::boolean OR to_tsvector('simple', a.title)
              @@ phraseto_tsquery('simple', 'с целью'))
        ORDER BY rank DESC, char_length(a.title)
        LIMIT 10
        """,
        server_id, cleaned, has_without, has_not, has_without_purpose, has_with_purpose,
        timeout=config.LAW_SEARCH_SQL_TIMEOUT,
    )
    return [r["id"] for r in rows]


async def _leg_phrase_title(server_id: str, cleaned: str) -> List[int]:
    """Буквальная title-нога для запросов с отрицанием («без ...», «не ...»).

    Russian FTS удаляет «без» как стоп-слово и делает противоположные формулировки
    «с целью хищения» / «без цели хищения» одинаковыми. Полная подстрока сохраняет
    отрицание и служит дополнительным, но только положительным сигналом.
    """
    if len(cleaned) < 4:
        return []
    rows = await _db_fetch(
        """
        SELECT id
        FROM law_articles
        WHERE server_id = $1 AND title IS NOT NULL
          AND position($2 IN lower(title)) > 0
        ORDER BY (lower(title) = $2) DESC, char_length(title), id
        LIMIT 10
        """,
        server_id, cleaned, timeout=config.LAW_SEARCH_SQL_TIMEOUT,
    )
    return [r["id"] for r in rows]


async def _leg_fts_variants(server_id: str, variants: List[str]) -> List[int]:
    """Строгий FTS по упорядоченным юридическим эквивалентам; без OR-добора."""
    variants = [v for v in variants if v]
    if not variants:
        return []
    rows = await _db_fetch(
        """
        WITH queries AS (
          SELECT ord, raw, websearch_to_tsquery('russian', raw) AS q,
                 to_tsvector('simple', raw) @@ plainto_tsquery('simple', 'без') AS has_without,
                 to_tsvector('simple', raw) @@ plainto_tsquery('simple', 'не') AS has_not,
                 to_tsvector('simple', raw) @@ phraseto_tsquery('simple', 'без цели') AS without_purpose,
                 to_tsvector('simple', raw) @@ phraseto_tsquery('simple', 'с целью') AS with_purpose
          FROM unnest($2::text[]) WITH ORDINALITY AS source(raw, ord)
        ), matches AS (
          SELECT a.id, queries.ord, ts_rank_cd(a.fts, queries.q, 33) AS rank
          FROM law_articles a CROSS JOIN queries
          WHERE a.server_id = $1 AND a.fts @@ queries.q
            AND (NOT queries.has_without OR to_tsvector('simple', coalesce(a.full_text, ''))
                @@ plainto_tsquery('simple', 'без'))
            AND (NOT queries.has_not OR to_tsvector('simple', coalesce(a.full_text, ''))
                @@ plainto_tsquery('simple', 'не'))
            AND (NOT queries.without_purpose OR to_tsvector('simple', coalesce(a.full_text, ''))
                @@ phraseto_tsquery('simple', 'без цели'))
            AND (NOT queries.with_purpose OR to_tsvector('simple', coalesce(a.full_text, ''))
                @@ phraseto_tsquery('simple', 'с целью'))
        ), first_supported AS (
          SELECT MIN(ord) AS ord FROM matches
        )
        SELECT matches.id
        FROM matches CROSS JOIN first_supported
        WHERE matches.ord = first_supported.ord
        ORDER BY matches.rank DESC, matches.id
        LIMIT 15
        """,
        server_id, variants, timeout=config.LAW_SEARCH_SQL_TIMEOUT,
    )
    return [r["id"] for r in rows]


async def _leg_title_variants(server_id: str, variants: List[str]) -> List[int]:
    """Title-нога для упорядоченных вариантов одного юридического понятия.

    Как и у intent-title, используем первый вариант, который реально существует на
    сервере. Так общий состав «автомобиль или иное ТС» не проигрывает более узкому
    «государственное ТС» из-за дополнительного случайного FTS-голоса.
    """
    variants = [v for v in variants if len(v) >= 2]
    if not variants:
        return []
    rows = await _db_fetch(
        """
        WITH queries AS (
          SELECT ord, raw, websearch_to_tsquery('russian', raw) AS q,
                 to_tsvector('simple', raw) @@ plainto_tsquery('simple', 'без') AS has_without,
                 to_tsvector('simple', raw) @@ plainto_tsquery('simple', 'не') AS has_not,
                 to_tsvector('simple', raw) @@ phraseto_tsquery('simple', 'без цели') AS without_purpose,
                 to_tsvector('simple', raw) @@ phraseto_tsquery('simple', 'с целью') AS with_purpose
          FROM unnest($2::text[]) WITH ORDINALITY AS source(raw, ord)
        ), matches AS (
          SELECT a.id, a.title, queries.ord,
                 ts_rank_cd(to_tsvector('russian', a.title), queries.q, 1) AS rank
          FROM law_articles a CROSS JOIN queries
          WHERE a.server_id = $1 AND a.title IS NOT NULL
            AND to_tsvector('russian', a.title) @@ queries.q
            AND (NOT queries.has_without OR to_tsvector('simple', a.title)
                @@ plainto_tsquery('simple', 'без'))
            AND (NOT queries.has_not OR to_tsvector('simple', a.title)
                @@ plainto_tsquery('simple', 'не'))
            AND (NOT queries.without_purpose OR to_tsvector('simple', a.title)
                @@ phraseto_tsquery('simple', 'без цели'))
            AND (NOT queries.with_purpose OR to_tsvector('simple', a.title)
                @@ phraseto_tsquery('simple', 'с целью'))
        ), first_supported AS (
          SELECT MIN(ord) AS ord FROM matches
        )
        SELECT matches.id
        FROM matches CROSS JOIN first_supported
        WHERE matches.ord = first_supported.ord
        ORDER BY matches.rank DESC, char_length(matches.title), matches.id
        LIMIT 10
        """,
        server_id, variants, timeout=config.LAW_SEARCH_SQL_TIMEOUT,
    )
    return [r["id"] for r in rows]


async def _leg_intent_phrase(server_id: str, variants: List[str]) -> List[int]:
    """Высокоточная full-text нога для смысловых аналогов материальной нормы.

    У части Majestic заголовок 16.15 был распознан как одни звёзды, поэтому title-only
    принципиально не видит норму. `phraseto_tsquery` ищет цельную юридическую формулировку
    в индексированном полном тексте; первый поддержанный вариант выбирается отдельно для
    каждого server_id. Та же фраза должна находиться в начале исходной статьи — это не даёт
    поздней случайной цитате стать ответом и не создаёт зависимости от LLM-обогащения.
    """
    variants = [v for v in variants if len(v) >= 2]
    if not variants:
        return []
    rows = await _db_fetch(
        """
        WITH queries AS (
          SELECT ord, phraseto_tsquery('russian', raw) AS q
          FROM unnest($2::text[]) WITH ORDINALITY AS source(raw, ord)
        ), matches AS (
          SELECT a.id, queries.ord, ts_rank_cd(a.fts, queries.q, 33) AS rank
          FROM law_articles a CROSS JOIN queries
          WHERE a.server_id = $1 AND a.kind = 'article' AND a.fts @@ queries.q
            AND to_tsvector('russian', left(coalesce(a.full_text, ''), 1000)) @@ queries.q
        ), first_supported AS (
          SELECT MIN(ord) AS ord FROM matches
        )
        SELECT matches.id
        FROM matches CROSS JOIN first_supported
        WHERE matches.ord = first_supported.ord
        ORDER BY matches.rank DESC, matches.id
        LIMIT 10
        """,
        server_id, variants, timeout=config.LAW_SEARCH_SQL_TIMEOUT,
    )
    return [r["id"] for r in rows]


async def _leg_trgm(server_id: str, cleaned: str) -> List[Tuple[int, float]]:
    if len(cleaned) < 3:
        return []
    rows = await _db_fetch(
        """
        SELECT id, similarity(lower(coalesce(title, '')), $2) AS sim
        FROM law_articles
        WHERE server_id = $1 AND lower(coalesce(title, '')) % $2
        ORDER BY sim DESC
        LIMIT 10
        """,
        server_id, cleaned, timeout=config.LAW_SEARCH_SQL_TIMEOUT,
    )
    return [(r["id"], float(r["sim"] or 0.0)) for r in rows]


async def _run_sql_leg(name: str, awaitable: Awaitable[List[int]]) -> _LegOutcome:
    """Одинаковая семантика всех SQL-ног: пустая выдача — не ошибка, timeout/error — ошибка."""
    try:
        ids = await asyncio.wait_for(awaitable, timeout=config.LAW_SEARCH_SQL_TIMEOUT)
    except asyncio.TimeoutError:
        logger.warning("[LAW_SEARCH] нога %s превысила SQL-таймаут", name)
        return _LegOutcome("timeout", warnings=[f"{name}_timeout"], degraded=True)
    except Exception:
        logger.exception("[LAW_SEARCH] нога %s упала", name)
        return _LegOutcome("error", warnings=[f"{name}_error"], degraded=True)
    return _LegOutcome("ok" if ids else "empty", ids=ids)


async def _run_scored_sql_leg(
    name: str,
    awaitable: Awaitable[List[Tuple[int, float]]],
) -> _LegOutcome:
    """SQL-нога, где rank недостаточно: сохраняем абсолютную confidence каждого hit."""
    try:
        hits = await asyncio.wait_for(awaitable, timeout=config.LAW_SEARCH_SQL_TIMEOUT)
    except asyncio.TimeoutError:
        logger.warning("[LAW_SEARCH] нога %s превысила SQL-таймаут", name)
        return _LegOutcome("timeout", warnings=[f"{name}_timeout"], degraded=True)
    except Exception:
        logger.exception("[LAW_SEARCH] нога %s упала", name)
        return _LegOutcome("error", warnings=[f"{name}_error"], degraded=True)
    ids = [article_id for article_id, _confidence in hits]
    confidences = {
        article_id: max(0.0, min(1.0, float(confidence)))
        for article_id, confidence in hits
    }
    return _LegOutcome(
        "ok" if ids else "empty",
        ids=ids,
        confidences=confidences,
        raw_scores=dict(confidences),
    )


async def _vector_corpus_state(server_id: str, space_id: str) -> _VectorCorpusState:
    """Покрытие именно активного пространства; legacy embedding_local сознательно не читается."""
    row = await _db_fetchrow(
        """
        SELECT
          count(*)::int AS total,
          count(*) FILTER (
            WHERE e.embedding IS NOT NULL AND e.input_hash IS NOT NULL
              AND a.embedding_local_space = $2
              AND e.input_hash = a.embedding_local_input_hash
              AND NOT EXISTS (
                SELECT 1 FROM law_embedding_jobs j
                WHERE j.article_id = a.id AND j.space_id = $2
                  AND NOT j.keep_current
              )
          )::int AS ready,
          count(*) FILTER (
            WHERE EXISTS (
              SELECT 1 FROM law_embedding_jobs j
              WHERE j.article_id = a.id AND j.space_id = $2
                AND j.status IN ('pending', 'processing')
                AND NOT j.keep_current
            )
          )::int AS queued,
          count(*) FILTER (
            WHERE EXISTS (
              SELECT 1 FROM law_embedding_jobs j
              WHERE j.article_id = a.id AND j.space_id = $2
                AND j.status = 'failed' AND NOT j.keep_current
            )
          )::int AS failed,
          count(*) FILTER (
            WHERE (
              e.article_id IS NULL AND EXISTS (
                SELECT 1 FROM law_article_embeddings other
                WHERE other.article_id = a.id AND other.space_id <> $2
              )
            ) OR (
              e.article_id IS NOT NULL AND (
                a.embedding_local_space IS DISTINCT FROM $2
                OR e.input_hash IS DISTINCT FROM a.embedding_local_input_hash
              )
            )
          )::int AS mismatched,
          (SELECT true FROM law_embedding_spaces s WHERE s.space_id = $2) AS passport_found,
          (SELECT s.model_name FROM law_embedding_spaces s WHERE s.space_id = $2) AS model_name,
          (SELECT s.model_revision FROM law_embedding_spaces s WHERE s.space_id = $2) AS model_revision,
          (SELECT s.dimensions FROM law_embedding_spaces s WHERE s.space_id = $2) AS dimensions,
          (SELECT s.query_prefix FROM law_embedding_spaces s WHERE s.space_id = $2) AS query_prefix,
          (SELECT s.document_prefix FROM law_embedding_spaces s WHERE s.space_id = $2) AS document_prefix,
          (SELECT s.pooling FROM law_embedding_spaces s WHERE s.space_id = $2) AS pooling,
          (SELECT s.normalized FROM law_embedding_spaces s WHERE s.space_id = $2) AS normalized,
          (SELECT s.recipe_version FROM law_embedding_spaces s WHERE s.space_id = $2) AS recipe_version,
          COALESCE((
            SELECT s.metadata->'runtime_versions' = $3::jsonb
            FROM law_embedding_spaces s WHERE s.space_id = $2
          ), false) AS runtime_compatible
        FROM law_articles a
        LEFT JOIN law_article_embeddings e
          ON e.article_id = a.id AND e.server_id = a.server_id AND e.space_id = $2
        WHERE a.server_id = $1 AND a.kind = 'article'
        """,
        server_id,
        space_id,
        json.dumps(config.LAW_CATALOG_EMBED_RUNTIME_VERSIONS, sort_keys=True),
        timeout=config.LAW_SEARCH_SQL_TIMEOUT,
    )
    return _VectorCorpusState(
        total=int(row["total"] or 0),
        ready=int(row["ready"] or 0),
        queued=int(row["queued"] or 0),
        failed=int(row["failed"] or 0),
        mismatched=int(row["mismatched"] or 0),
        passport_found=bool(row["passport_found"]),
        model_name=row["model_name"],
        model_revision=row["model_revision"],
        dimensions=int(row["dimensions"]) if row["dimensions"] is not None else None,
        query_prefix=row["query_prefix"],
        document_prefix=row["document_prefix"],
        pooling=row["pooling"],
        normalized=row["normalized"],
        recipe_version=row["recipe_version"],
        runtime_compatible=bool(row["runtime_compatible"]),
    )


async def _embed_frida_query(text: str, cache_key: str) -> Tuple[LegStatus, Optional[List[float]]]:
    """Короткий bounded-вызов FRIDA; слот освобождается только когда CPU-поток реально закончил."""
    from services.law_embedder import get_law_embedder

    vec = _cache_get(cache_key)
    if vec is not None and len(vec) == config.LAW_CATALOG_EMBED_DIMS:
        return "ok", vec
    if vec is not None:
        _EMBED_CACHE.pop(cache_key, None)

    emb = get_law_embedder()
    if emb is None:
        logger.warning("[LAW_SEARCH] FRIDA не загружена — keyword-only")
        return "error", None
    if emb.dims is not None and emb.dims != config.LAW_CATALOG_EMBED_DIMS:
        logger.error("[LAW_SEARCH] FRIDA dims=%s, ожидается %s", emb.dims, config.LAW_CATALOG_EMBED_DIMS)
        return "error", None

    loop = asyncio.get_running_loop()
    deadline = loop.time() + config.LAW_SEARCH_EMBED_TIMEOUT
    slots = _frida_slots()
    try:
        await asyncio.wait_for(slots.acquire(), timeout=max(0.001, deadline - loop.time()))
    except asyncio.TimeoutError:
        logger.warning("[LAW_SEARCH] FRIDA занята дольше таймаута — keyword-only")
        return "timeout", None

    # Другой запрос мог успеть посчитать тот же ключ, пока мы ждали слот.
    vec = _cache_get(cache_key)
    if vec is not None and len(vec) == config.LAW_CATALOG_EMBED_DIMS:
        slots.release()
        return "ok", vec

    try:
        task = asyncio.create_task(emb.embed_query(text))
    except Exception:
        slots.release()
        raise

    def _finish(done: "asyncio.Task[List[float]]") -> None:
        try:
            result = done.result()
            if result and len(result) == config.LAW_CATALOG_EMBED_DIMS:
                _cache_put(cache_key, result)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.warning("[LAW_SEARCH] фоновое завершение FRIDA упало", exc_info=True)
        finally:
            slots.release()

    task.add_done_callback(_finish)
    try:
        vec = await asyncio.wait_for(
            asyncio.shield(task),
            timeout=max(0.001, deadline - loop.time()),
        )
    except asyncio.TimeoutError:
        # asyncio.to_thread нельзя надёжно остановить. shield оставляет CPU-задачу работать, а
        # _finish удерживает слот до её настоящего конца — новые запросы не создают очередь без границ.
        logger.warning("[LAW_SEARCH] FRIDA превысила %.2fs — keyword-only", config.LAW_SEARCH_EMBED_TIMEOUT)
        return "timeout", None
    except Exception:
        logger.warning("[LAW_SEARCH] FRIDA embed упал — keyword-only", exc_info=True)
        return "error", None
    if not vec or len(vec) != config.LAW_CATALOG_EMBED_DIMS:
        logger.error("[LAW_SEARCH] FRIDA вернула пустой/несовместимый вектор (dims=%s)", len(vec or []))
        return "error", None
    return "ok", vec


async def _leg_vector_local(server_id: str, vector_text: str) -> _LegOutcome:
    """Локальная FRIDA + canonical multi-space таблица векторов."""
    space_id = config.LAW_CATALOG_EMBED_SPACE_ID
    warnings: List[str] = []
    try:
        state = await asyncio.wait_for(
            _vector_corpus_state(server_id, space_id),
            timeout=config.LAW_SEARCH_SQL_TIMEOUT,
        )
    except asyncio.TimeoutError:
        logger.warning("[LAW_SEARCH] проверка FRIDA-корпуса превысила SQL-таймаут")
        return _LegOutcome("timeout", warnings=["vector_corpus_timeout"], degraded=True)
    except Exception:
        logger.exception("[LAW_SEARCH] не удалось проверить FRIDA-корпус")
        return _LegOutcome("error", warnings=["vector_corpus_error"], degraded=True)

    if not state.compatible:
        logger.error("[LAW_SEARCH] паспорт FRIDA-space %s отсутствует или несовместим", space_id)
        return _LegOutcome("skipped", warnings=["vector_space_mismatch"], degraded=True)
    if not state.complete:
        warnings.append("vector_corpus_incomplete")
    if state.mismatched:
        warnings.append("vector_space_mismatch")
    if state.failed:
        warnings.append("vector_jobs_failed")
    corpus_degraded = bool(warnings)

    if state.total == 0:
        return _LegOutcome("empty", warnings=warnings, degraded=corpus_degraded, usable=True)
    # ANN по 5-10% корпуса не является «частично полезным»: отсутствующие статьи вообще
    # не могут попасть в top, а случайный ready-поднабор получает незаслуженный vector boost.
    # Инкрементальный rebuild с keep_current не мешает complete: старый совместимый вектор
    # остаётся валидным до атомарной замены новым.
    if not state.complete:
        return _LegOutcome("skipped", warnings=warnings, degraded=True, usable=False)

    mode = config.LAW_VECTOR_QUERY_MODE
    cache_key = f"local::{space_id}::{mode}::{vector_text.lower().strip()}"
    embed_status, vec = await _embed_frida_query(vector_text, cache_key)
    if embed_status != "ok" or vec is None:
        warning = "vector_timeout" if embed_status == "timeout" else "vector_unavailable"
        return _LegOutcome(embed_status, warnings=[*warnings, warning], degraded=True)

    # Между первой проверкой и окончанием CPU-инференса каталог мог начать rebuild.
    # Повторная проверка закрывает это окно до ANN; SQL ниже дополнительно содержит
    # атомарный guard в собственном snapshot.
    try:
        refreshed = await asyncio.wait_for(
            _vector_corpus_state(server_id, space_id),
            timeout=config.LAW_SEARCH_SQL_TIMEOUT,
        )
    except asyncio.TimeoutError:
        return _LegOutcome("timeout", warnings=[*warnings, "vector_corpus_timeout"], degraded=True)
    except Exception:
        logger.exception("[LAW_SEARCH] повторная проверка FRIDA-корпуса упала")
        return _LegOutcome("error", warnings=[*warnings, "vector_corpus_error"], degraded=True)
    if not refreshed.compatible or not refreshed.complete:
        refreshed_warnings = [*warnings]
        if not refreshed.compatible or refreshed.mismatched:
            refreshed_warnings.append("vector_space_mismatch")
        if not refreshed.complete:
            refreshed_warnings.append("vector_corpus_incomplete")
        if refreshed.failed:
            refreshed_warnings.append("vector_jobs_failed")
        return _LegOutcome(
            "skipped", warnings=_dedupe_warnings(refreshed_warnings),
            degraded=True, usable=False,
        )

    try:
        rows = await asyncio.wait_for(
            _db_fetch(
                f"""
                WITH corpus_ready AS (
                  SELECT NOT EXISTS (
                    SELECT 1
                    FROM law_articles corpus
                    LEFT JOIN law_article_embeddings current
                      ON current.article_id = corpus.id
                     AND current.server_id = corpus.server_id
                     AND current.space_id = $2
                    WHERE corpus.server_id = $1 AND corpus.kind = 'article'
                      AND (
                        current.article_id IS NULL
                        OR current.embedding IS NULL
                        OR current.input_hash IS NULL
                        OR corpus.embedding_local_space IS DISTINCT FROM $2
                        OR current.input_hash IS DISTINCT FROM corpus.embedding_local_input_hash
                        OR EXISTS (
                          SELECT 1 FROM law_embedding_jobs corpus_job
                          WHERE corpus_job.article_id = corpus.id
                            AND corpus_job.space_id = $2
                            AND NOT corpus_job.keep_current
                        )
                      )
                  ) AS ok
                )
                SELECT corpus_ready.ok AS corpus_ready, hit.id, hit.distance
                FROM corpus_ready
                LEFT JOIN LATERAL (
                  SELECT e.article_id AS id, e.embedding <=> $3::vector AS distance
                  FROM law_article_embeddings e
                  JOIN law_articles a ON a.id = e.article_id
                  WHERE corpus_ready.ok
                    AND e.server_id = $1 AND a.server_id = $1
                    AND e.space_id = $2 AND a.embedding_local_space = $2
                    AND a.kind = 'article'
                    AND e.embedding IS NOT NULL AND e.input_hash IS NOT NULL
                    AND e.input_hash = a.embedding_local_input_hash
                    AND NOT EXISTS (
                      SELECT 1 FROM law_embedding_jobs j
                      WHERE j.article_id = e.article_id AND j.space_id = e.space_id
                        AND NOT j.keep_current
                    )
                    AND (1 - (e.embedding <=> $3::vector)) >= $4
                  ORDER BY e.embedding <=> $3::vector
                  LIMIT 10
                ) hit ON TRUE
                ORDER BY hit.distance NULLS LAST
                """,
                server_id, space_id, _to_pgvector(vec), config.LAW_VECTOR_MIN_SIM,
                timeout=config.LAW_SEARCH_SQL_TIMEOUT,
                # pgvector 0.8+ keeps scanning the global HNSW after the
                # server/space filter removes candidates. Without this a
                # populated server can spuriously return fewer than LIMIT.
                local_settings={"hnsw.iterative_scan": "relaxed_order"},
            ),
            timeout=config.LAW_SEARCH_SQL_TIMEOUT,
        )
    except asyncio.TimeoutError:
        logger.warning("[LAW_SEARCH] vector SQL превысил таймаут")
        return _LegOutcome("timeout", warnings=[*warnings, "vector_timeout"], degraded=True)
    except Exception:
        logger.exception("[LAW_SEARCH] vector SQL упал")
        return _LegOutcome("error", warnings=[*warnings, "vector_error"], degraded=True)
    if rows and not bool(rows[0]["corpus_ready"]):
        return _LegOutcome(
            "skipped",
            warnings=_dedupe_warnings([*warnings, "vector_corpus_incomplete"]),
            degraded=True,
            usable=False,
        )
    ids = [r["id"] for r in rows if r["id"] is not None]
    raw_scores: Dict[int, float] = {}
    for row in rows:
        if row["id"] is None:
            continue
        try:
            distance = row["distance"]
        except (KeyError, TypeError):
            continue
        if distance is not None:
            raw_scores[int(row["id"])] = 1.0 - float(distance)
    return _LegOutcome(
        "ok" if ids else "empty",
        ids=ids,
        raw_scores=raw_scores,
        warnings=warnings,
        degraded=corpus_degraded,
        usable=True,
    )


# --- Слияние и карточки --------------------------------------------------------------------------

_RRF_K = 60
_AMBIGUOUS_CODE_WEIGHT = 0.65
_RERANK_POOL_SIZE = 20
_LEG_WEIGHTS = {
    "code": 2.0,
    # Explicit structural marker + exact code + complete same-kind title is
    # authoritative and also receives deterministic priority below.
    "explicit_code_title": 2.0,
    # Same-server exact code + complete normalized title is strong combined
    # evidence, but remains unprotected and outside `_EXACT_LEGS`.
    "code_title_rescue": 2.0,
    "title": 2.0,
    "phrase_title": 2.25,
    "legal_title": 2.0,
    # Владелец домена явно определил эту ситуационную норму как top-1. Нога при этом
    # строгая: цельная фраза + санкция + тот же server_id, поэтому её одного голоса
    # достаточно, чтобы не проиграть сумме рыхлых FTS/trigram/auto-synonym сигналов.
    "intent_phrase": 4.0,
    "synonym_exact": 2.0,
    "synonym": 1.5,
    "legal_fts": 1.25,
    "fts": 1.0,
    "trgm": 0.8,
    "vector": 1.0,
}


_EXACT_LEGS = (
    "code", "explicit_code_title", "title", "phrase_title", "synonym_exact",
)  # только прямые совпадения исходного запроса; synthetic rewrite сюда не входит


def _fuse(legs: Dict[str, List[int]],
          weights: Optional[Dict[str, float]] = None,
          confidences: Optional[Dict[str, Dict[int, float]]] = None,
          ) -> List[Tuple[int, float, List[str]]]:
    """Reciprocal Rank Fusion: score = Σ weight/(k + rank). Возвращает [(id, score, [ноги])].
    weights — опц. переопределение _LEG_WEIGHTS (для условного буста вектора).
    confidences сохраняет абсолютную силу fuzzy-hit; у rank-only ног она равна 1."""
    wmap = weights or _LEG_WEIGHTS
    confidence_map = confidences or {}
    scores: Dict[int, float] = {}
    sources: Dict[int, List[str]] = {}
    for leg, ids in legs.items():
        w = wmap.get(leg, 1.0)
        for rank, aid in enumerate(dict.fromkeys(ids)):
            confidence = max(0.0, min(1.0, confidence_map.get(leg, {}).get(aid, 1.0)))
            if confidence <= 0:
                continue
            scores[aid] = scores.get(aid, 0.0) + (w * confidence) / (_RRF_K + rank + 1)
            sources.setdefault(aid, []).append(leg)
    return sorted(
        ((aid, sc, sources[aid]) for aid, sc in scores.items()),
        key=lambda x: (-x[1], x[0]),
    )


def _prioritize_ids(
    fused: List[Tuple[int, float, List[str]]],
    priority_ids: List[int],
) -> List[Tuple[int, float, List[str]]]:
    """Стабильно закрепляет доказанные intent-hit перед обычным RRF-хвостом."""
    order = {article_id: rank for rank, article_id in enumerate(dict.fromkeys(priority_ids))}
    if not order:
        return fused
    original = {article_id: rank for rank, (article_id, _score, _sources) in enumerate(fused)}
    return sorted(
        fused,
        key=lambda item: (
            0 if item[0] in order else 1,
            order.get(item[0], original[item[0]]),
        ),
    )


def _prioritize_applicability(
    fused: List[Tuple[int, float, List[str]]],
    evidence: Mapping[int, Mapping[str, Any]],
    lexical_evidence: Optional[Mapping[int, Mapping[str, Any]]] = None,
    protected_ids: Optional[set[int]] = None,
) -> List[Tuple[int, float, List[str]]]:
    """Move a typed match only inside a source-backed lexical cohort.

    A numeric interval is not enough on its own: a remote article can mention
    the same unit while regulating another subject.  A match may pass an
    earlier mismatch only when both candidates have direct article evidence,
    and the match retains every direct and structural query lexeme supported
    by that earlier candidate. Unknown applicability is deliberately left in
    its original RRF position.
    """

    if not any(
        row.get("verdict") == "match"
        for row in evidence.values()
    ):
        return fused
    if not lexical_evidence:
        return fused

    def terms(article_id: int) -> frozenset[str]:
        return frozenset(
            str(term)
            for term in lexical_evidence.get(article_id, {}).get(
                "matched_terms",
                [],
            )
        )

    def direct_terms(article_id: int) -> frozenset[str]:
        return frozenset(
            str(term)
            for term in lexical_evidence.get(article_id, {}).get(
                "direct_terms",
                [],
            )
        )

    def is_subject_term(term: str) -> bool:
        if term in {
            "час", "день", "дн", "ден", "дня", "дне",
            "срок", "вес", "сумм", "размер",
        }:
            return False
        return not term.startswith(
            (
                "грам", "килограмм", "миллиграм",
                "доллар", "процент", "голос",
                "сут",
                "сотн", "тысяч", "миллион", "миллиард", "триллион",
                "масс", "стоимост",
                "количеств", "размер",
            )
        )

    protected_ids = protected_ids or set()
    ordered = list(fused)
    for index in range(1, len(ordered)):
        article_id = ordered[index][0]
        if evidence.get(article_id, {}).get("verdict") != "match":
            continue
        match_terms = terms(article_id)
        match_direct_terms = direct_terms(article_id)
        if not match_terms or not match_direct_terms:
            continue
        target = index
        while target > 0:
            previous_id = ordered[target - 1][0]
            if (
                previous_id in protected_ids
                and article_id not in protected_ids
            ):
                break
            if evidence.get(previous_id, {}).get("verdict") != "mismatch":
                break
            previous_terms = terms(previous_id)
            previous_direct_terms = direct_terms(previous_id)
            shared_direct_terms = (
                match_direct_terms & previous_direct_terms
            )
            if (
                not previous_terms
                or not previous_direct_terms
                or not match_terms.issuperset(previous_terms)
                or not match_direct_terms.issuperset(previous_direct_terms)
                or not any(
                    is_subject_term(term)
                    for term in shared_direct_terms
                )
            ):
                break
            ordered[target - 1], ordered[target] = (
                ordered[target],
                ordered[target - 1],
            )
            target -= 1
    return ordered


def _snippet(text: str, limit: int = 400) -> str:
    text = re.sub(r'\s+', ' ', (text or '')).strip()
    return text if len(text) <= limit else text[:limit].rsplit(' ', 1)[0] + '…'


async def _fetch_cards(ids: List[int]) -> Dict[int, Dict[str, Any]]:
    if not ids:
        return {}
    rows = await _db_fetch(
        """
        SELECT id, law_document_id, server_id, article_ref, kind, code, title, doc_title,
               penalty_summary, penalty_json, full_text
        FROM law_articles WHERE id = ANY($1)
        """,
        ids, timeout=config.LAW_SEARCH_CARDS_TIMEOUT,
    )
    return {r["id"]: dict(r) for r in rows}


async def _fetch_ranking_evidence(
    server_id: str,
    ids: List[int],
    query_text: str,
) -> Dict[int, Dict[str, Any]]:
    """PostgreSQL-stemmed source/synonym/ancestor evidence for a small top-N pool."""

    if not ids or not query_text:
        return {}
    rows = await _db_fetch(
        """
        WITH query_data AS MATERIALIZED (
          SELECT tsvector_to_array(
            to_tsvector('russian', $3)
          ) AS terms
        ),
        candidate_base AS MATERIALIZED (
          SELECT article.*
          FROM law_articles article
          WHERE article.server_id = $1
            AND article.id = ANY($2::int[])
        ),
        document_orientation AS MATERIALIZED (
          SELECT
            documents.law_document_id,
            CASE
              WHEN min(structural.source_order)
                     FILTER (WHERE structural.kind = 'section') IS NULL
               AND min(structural.source_order)
                     FILTER (WHERE structural.kind = 'chapter') IS NULL
                THEN 'none'
              WHEN min(structural.source_order)
                     FILTER (WHERE structural.kind = 'chapter') IS NULL
                THEN 'section_only'
              WHEN min(structural.source_order)
                     FILTER (WHERE structural.kind = 'section') IS NULL
                THEN 'chapter_only'
              WHEN min(structural.source_order)
                     FILTER (WHERE structural.kind = 'section')
                   < min(structural.source_order)
                     FILTER (WHERE structural.kind = 'chapter')
                THEN 'section_parent'
              WHEN min(structural.source_order)
                     FILTER (WHERE structural.kind = 'chapter')
                   < min(structural.source_order)
                     FILTER (WHERE structural.kind = 'section')
                THEN 'chapter_parent'
              ELSE 'ambiguous'
            END AS orientation
          FROM (
            SELECT DISTINCT law_document_id
            FROM candidate_base
          ) documents
          LEFT JOIN law_articles structural
            ON structural.law_document_id = documents.law_document_id
           AND structural.kind IN ('chapter', 'section')
          GROUP BY documents.law_document_id
        ),
        candidates AS MATERIALIZED (
          SELECT
            article.id,
            orientation.orientation AS context_orientation,
            to_tsvector(
              'russian',
              concat_ws(' ', article.title, article.full_text)
            ) AS direct_evidence,
            to_tsvector(
              'russian',
              concat_ws(
                ' ',
                article.doc_title,
                CASE
                  WHEN orientation.orientation IN (
                    'chapter_parent',
                    'chapter_only'
                  )
                    THEN chapter.title
                  WHEN orientation.orientation = 'section_parent'
                   AND chapter.source_order > section.source_order
                    THEN chapter.title
                END,
                CASE
                  WHEN orientation.orientation IN (
                    'section_parent',
                    'section_only'
                  )
                    THEN section.title
                  WHEN orientation.orientation = 'chapter_parent'
                   AND section.source_order > chapter.source_order
                    THEN section.title
                END
              )
            ) AS context_evidence,
            to_tsvector(
              'russian',
              coalesce(synonyms.terms, '')
            ) AS synonym_evidence
          FROM candidate_base article
          JOIN document_orientation orientation
            ON orientation.law_document_id = article.law_document_id
          LEFT JOIN LATERAL (
            SELECT parent.title, parent.source_order
            FROM law_articles parent
            WHERE parent.law_document_id = article.law_document_id
              AND parent.kind = 'chapter'
              AND article.kind NOT IN ('chapter', 'section')
              AND parent.source_order < article.source_order
            ORDER BY parent.source_order DESC
            LIMIT 1
          ) chapter ON TRUE
          LEFT JOIN LATERAL (
            SELECT parent.title, parent.source_order
            FROM law_articles parent
            WHERE parent.law_document_id = article.law_document_id
              AND parent.kind = 'section'
              AND article.kind NOT IN ('chapter', 'section')
              AND parent.source_order < article.source_order
            ORDER BY parent.source_order DESC
            LIMIT 1
          ) section ON TRUE
          LEFT JOIN LATERAL (
            SELECT string_agg(synonym.term, ' ' ORDER BY synonym.id) AS terms
            FROM law_synonyms synonym
            WHERE synonym.server_id = article.server_id
              AND synonym.article_id = article.id
              AND synonym.status = 'approved'
          ) synonyms ON TRUE
        )
        SELECT
          candidates.id,
          candidates.context_orientation,
          query_data.terms AS query_terms,
          ARRAY(
            SELECT term
            FROM unnest(query_data.terms) term
            WHERE term = ANY(tsvector_to_array(candidates.direct_evidence))
               OR term = ANY(tsvector_to_array(candidates.context_evidence))
            ORDER BY term
          ) AS matched_terms,
          ARRAY(
            SELECT term
            FROM unnest(query_data.terms) term
            WHERE term = ANY(tsvector_to_array(candidates.direct_evidence))
            ORDER BY term
          ) AS direct_terms,
          ARRAY(
            SELECT term
            FROM unnest(query_data.terms) term
            WHERE term = ANY(tsvector_to_array(candidates.context_evidence))
            ORDER BY term
          ) AS context_terms,
          ARRAY(
            SELECT term
            FROM unnest(query_data.terms) term
            WHERE term = ANY(tsvector_to_array(candidates.synonym_evidence))
            ORDER BY term
          ) AS synonym_terms
        FROM candidates
        CROSS JOIN query_data
        ORDER BY candidates.id
        """,
        server_id,
        ids,
        query_text,
        timeout=config.LAW_SEARCH_SQL_TIMEOUT,
    )
    return {
        int(row["id"]): {
            "context_orientation": row["context_orientation"],
            "query_terms": list(row["query_terms"] or []),
            "matched_terms": list(row["matched_terms"] or []),
            "direct_terms": list(row["direct_terms"] or []),
            "context_terms": list(row["context_terms"] or []),
            "synonym_terms": list(row["synonym_terms"] or []),
        }
        for row in rows
    }


async def _fetch_related(article_id: int, limit: int = 5) -> Tuple[bool, List[Dict[str, Any]]]:
    rows = await _db_fetch(
        """
        WITH source AS (
          SELECT src.server_id,
            (EXISTS (
              SELECT 1
              FROM law_related_rebuild_jobs dirty
              WHERE dirty.server_id = src.server_id AND dirty.space_id = $3
            ) OR EXISTS (
              SELECT 1
              FROM law_embedding_jobs pending_vector
              WHERE pending_vector.server_id = src.server_id
                AND pending_vector.space_id = $3
                AND NOT pending_vector.keep_current
            )) AS dirty
          FROM law_articles src
          WHERE src.id = $1
        )
        SELECT related.id, related.code, related.title, related.doc_title,
               related.penalty_summary, related.full_text, related.score, source.dirty
        FROM source
        LEFT JOIN LATERAL (
          SELECT a.id, a.code, a.title, a.doc_title, a.penalty_summary,
                 left(a.full_text, 1000) AS full_text, r.score
          FROM law_article_related r
          JOIN law_articles a
            ON a.id = r.related_id AND a.server_id = source.server_id
          WHERE r.article_id = $1 AND r.vector_space = $3 AND NOT source.dirty
          ORDER BY r.score DESC
          LIMIT $2
        ) related ON TRUE
        ORDER BY related.score DESC NULLS LAST
        """,
        article_id, limit, config.LAW_CATALOG_EMBED_SPACE_ID,
        timeout=config.LAW_SEARCH_RELATED_TIMEOUT,
    )
    dirty = bool(rows and rows[0]["dirty"])
    related: List[Dict[str, Any]] = []
    for row in rows:
        if row["id"] is None:
            continue
        item = {key: value for key, value in dict(row).items() if key != "dirty"}
        item["title"] = recover_rating_metadata_title(item.get("title"), item.get("full_text"))
        item.pop("full_text", None)
        related.append(item)
    return dirty, related


def _card(row: Dict[str, Any], score: float, sources: List[str]) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "code": row.get("code"),
        "kind": row.get("kind"),
        "title": recover_rating_metadata_title(row.get("title"), row.get("full_text")),
        "doc_title": row.get("doc_title"),
        "article_ref": row.get("article_ref"),
        "penalty_summary": row.get("penalty_summary"),
        "snippet": _snippet(row.get("full_text") or ""),
        "score": round(score, 5),
        "sources": sources,
    }


def _dedupe_warnings(warnings: List[str]) -> List[str]:
    return list(dict.fromkeys(warnings))


def _response(
    nq: NormalizedQuery,
    results: Optional[List[Dict[str, Any]]] = None,
    related: Optional[List[Dict[str, Any]]] = None,
    *,
    degraded: bool = False,
    search_mode: Literal["full", "partial", "keyword_only"] = "full",
    warnings: Optional[List[str]] = None,
) -> Dict[str, Any]:
    return {
        "query": nq.original,
        "results": results or [],
        "related": related or [],
        "degraded": degraded,
        "search_mode": search_mode,
        "warnings": _dedupe_warnings(warnings or []),
    }


async def _cards_with_timeout(ids: List[int]) -> Tuple[LegStatus, Dict[int, Dict[str, Any]]]:
    if not ids:
        return "empty", {}
    try:
        cards = await asyncio.wait_for(_fetch_cards(ids), timeout=config.LAW_SEARCH_CARDS_TIMEOUT)
    except asyncio.TimeoutError:
        logger.warning("[LAW_SEARCH] загрузка карточек превысила таймаут")
        return "timeout", {}
    except Exception:
        logger.exception("[LAW_SEARCH] загрузка карточек упала")
        return "error", {}
    return ("ok" if cards else "empty"), cards


async def _ranking_evidence_with_timeout(
    server_id: str,
    ids: List[int],
    query_text: str,
) -> Tuple[LegStatus, Dict[int, Dict[str, Any]]]:
    if not ids or not query_text:
        return "empty", {}
    try:
        evidence = await asyncio.wait_for(
            _fetch_ranking_evidence(server_id, ids, query_text),
            timeout=config.LAW_SEARCH_SQL_TIMEOUT,
        )
    except asyncio.TimeoutError:
        logger.warning("[LAW_SEARCH] evidence rerank превысил SQL-таймаут")
        return "timeout", {}
    except Exception:
        logger.exception("[LAW_SEARCH] evidence rerank упал")
        return "error", {}
    return ("ok" if evidence else "empty"), evidence


async def _related_with_timeout(article_id: int) -> Tuple[LegStatus, List[Dict[str, Any]]]:
    try:
        dirty, related = await asyncio.wait_for(
            _fetch_related(article_id),
            timeout=config.LAW_SEARCH_RELATED_TIMEOUT,
        )
    except asyncio.TimeoutError:
        logger.warning("[LAW_SEARCH] related превысил таймаут; основные карточки сохранены")
        return "timeout", []
    except Exception:
        logger.exception("[LAW_SEARCH] related упал; основные карточки сохранены")
        return "error", []
    if dirty:
        return "skipped", []
    return ("ok" if related else "empty"), related


def _mode(vector_requested: bool, vector: _LegOutcome, degraded: bool) -> Literal["full", "partial", "keyword_only"]:
    if vector_requested and not vector.usable:
        return "keyword_only"
    return "partial" if degraded else "full"


async def _search_impl(
    server_id: str,
    nq: NormalizedQuery,
    limit: int = 8,
    use_vector: bool = True,
    diagnostics: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Реализация под верхним дедлайном search(); все ноги имеют собственные outcome/timeout."""
    if not nq.cleaned and not nq.codes:
        if diagnostics is not None:
            diagnostics["search_path"] = "empty_query"
            diagnostics["legs"] = {}
        return _response(nq, search_mode="full")

    warnings: List[str] = []
    degraded = False
    requested_code_kind = _requested_code_kind(nq)
    explicit_candidate = _explicit_code_title_candidate(nq)
    explicit_outcome = _LegOutcome("skipped")
    if explicit_candidate is not None:
        explicit_code, explicit_title, explicit_kind = explicit_candidate
        explicit_outcome = await _timed_outcome(
            _run_sql_leg(
                "explicit_code_title",
                _leg_explicit_code_title(
                    server_id,
                    explicit_code,
                    explicit_title,
                    explicit_kind,
                ),
            )
        )
        if diagnostics is not None:
            diagnostics.setdefault("legs", {})[
                "explicit_code_title"
            ] = _leg_diagnostics(explicit_outcome)
        warnings.extend(explicit_outcome.warnings)
        degraded = degraded or explicit_outcome.degraded
        if explicit_outcome.status in ("timeout", "error"):
            # Falling back to an ordinary code/doc result is unsafe here:
            # another document can legitimately have the same code while the
            # complete requested title identifies a different article.
            raise LawSearchUnavailable(
                f"explicit_code_title_{explicit_outcome.status}"
            )
        if explicit_outcome.ids:
            if diagnostics is not None:
                diagnostics["search_path"] = "explicit_code_title"
                diagnostics["priority_ids"] = list(
                    explicit_outcome.ids[:20]
                )
            top_n = explicit_outcome.ids[:max(1, min(limit, 20))]
            cards_status, cards_by_id = await _cards_with_timeout(top_n)
            if diagnostics is not None:
                diagnostics["cards_status"] = cards_status
            if cards_status in ("timeout", "error"):
                raise LawSearchUnavailable(f"cards_{cards_status}")
            cards = [
                _card(
                    cards_by_id[article_id],
                    1.0 / (1 + rank),
                    ["explicit_code_title"],
                )
                for rank, article_id in enumerate(top_n)
                if article_id in cards_by_id
            ]
            if not cards:
                raise LawSearchUnavailable("cards_unavailable")
            if len(cards) != len(top_n):
                warnings.append("cards_incomplete")
                degraded = True
            top_ids = {card["id"] for card in cards}
            related_status, raw_related = await _related_with_timeout(
                cards[0]["id"]
            )
            if diagnostics is not None:
                diagnostics["related_status"] = related_status
            if related_status in ("timeout", "error", "skipped"):
                warnings.append(
                    "related_rebuilding"
                    if related_status == "skipped"
                    else f"related_{related_status}"
                )
                degraded = True
            related = [
                row
                for row in raw_related
                if row["id"] not in top_ids
            ]
            logger.info(
                "[LAW_SEARCH] server=%s q=%r → точный kind/code/title: %d норм",
                server_id,
                nq.original[:60],
                len(cards),
            )
            return _response(
                nq,
                cards,
                related,
                degraded=degraded,
                search_mode="partial" if degraded else "full",
                warnings=warnings,
            )

    # Целевой запрос «статья N <документ>» разрешается в два независимых шага. Сначала
    # фиксируем документ только по его названию, не глядя на наличие нужного code/kind.
    # Затем ищем норму строго внутри найденного document scope. Это не позволяет запросу
    # «глава 1 Административного кодекса» тихо уехать в главу 1 другого кодекса, если в
    # Административном кодексе такой главы нет. Если название документа вообще не
    # распознано (обычный концепт вроде «5 разбой»), сохраняется общий hybrid pipeline.
    if _should_resolve_document_scope(nq, requested_code_kind):
        scope_qualifier = _document_scope_qualifier(nq, requested_code_kind)
        kw = _doc_keywords(scope_qualifier)
        scope_outcome = (
            await _timed_outcome(
                _run_sql_leg(
                    "code_doc_scope",
                    _leg_document_scope(server_id, scope_qualifier, kw),
                )
            )
            if kw else _LegOutcome("skipped")
        )
        if diagnostics is not None:
            diagnostics.setdefault("legs", {})["code_doc_scope"] = _leg_diagnostics(
                scope_outcome
            )
        warnings.extend(scope_outcome.warnings)
        degraded = degraded or scope_outcome.degraded
        if scope_outcome.status in ("timeout", "error"):
            raise LawSearchUnavailable(f"code_doc_scope_{scope_outcome.status}")

        document_scope_ids = list(scope_outcome.ids)
        if document_scope_ids:
            authoritative_scope_outcome = await _timed_outcome(
                _run_sql_leg(
                    "code_doc_scope_authoritative",
                    _leg_document_scope_authoritative(
                        server_id,
                        document_scope_ids,
                        scope_qualifier,
                        kw,
                    ),
                )
            )
            if diagnostics is not None:
                diagnostics.setdefault("legs", {})[
                    "code_doc_scope_authoritative"
                ] = _leg_diagnostics(authoritative_scope_outcome)
            warnings.extend(authoritative_scope_outcome.warnings)
            degraded = degraded or authoritative_scope_outcome.degraded
            if authoritative_scope_outcome.status in ("timeout", "error"):
                raise LawSearchUnavailable(
                    f"code_doc_scope_authoritative_{authoritative_scope_outcome.status}"
                )
            document_scope_ids = list(authoritative_scope_outcome.ids)
            if not document_scope_ids:
                logger.info(
                    "[LAW_SEARCH] server=%s q=%r → частичное совпадение названия документа; "
                    "сохраняем hybrid (документы=%s, keywords=%s)",
                    server_id,
                    nq.original[:60],
                    scope_outcome.ids,
                    kw,
                )

        doc_outcome = (
            await _timed_outcome(
                _run_sql_leg(
                    "code_doc",
                    _leg_code_doc(
                        server_id,
                        nq.codes,
                        scope_qualifier,
                        kw,
                        fallback_codes=nq.dash_fallback_codes,
                        requested_kind=requested_code_kind,
                        document_ids=document_scope_ids,
                    ),
                )
            )
            if document_scope_ids else _LegOutcome("skipped")
        )
        if diagnostics is not None:
            diagnostics.setdefault("legs", {})["code_doc"] = _leg_diagnostics(doc_outcome)
        warnings.extend(doc_outcome.warnings)
        degraded = degraded or doc_outcome.degraded
        if doc_outcome.status in ("timeout", "error"):
            raise LawSearchUnavailable(f"code_doc_{doc_outcome.status}")
        doc_ids = doc_outcome.ids
        if doc_ids:
            if diagnostics is not None:
                diagnostics["search_path"] = "code_doc"
            top_n = doc_ids[:max(1, min(limit, 20))]
            cards_status, cards_by_id = await _cards_with_timeout(top_n)
            if diagnostics is not None:
                diagnostics["cards_status"] = cards_status
            if cards_status in ("timeout", "error"):
                raise LawSearchUnavailable(f"cards_{cards_status}")
            cards = [_card(cards_by_id[a], 1.0 / (1 + i), ["code"])
                     for i, a in enumerate(top_n) if a in cards_by_id]
            if not cards:
                raise LawSearchUnavailable("cards_unavailable")
            if len(cards) != len(top_n):
                warnings.append("cards_incomplete")
                degraded = True
            related: List[Dict[str, Any]] = []
            if cards:
                top_ids = {c["id"] for c in cards}
                related_status, raw_related = await _related_with_timeout(cards[0]["id"])
                if diagnostics is not None:
                    diagnostics["related_status"] = related_status
                if related_status in ("timeout", "error", "skipped"):
                    warnings.append(
                        "related_rebuilding"
                        if related_status == "skipped"
                        else f"related_{related_status}"
                    )
                    degraded = True
                related = [r for r in raw_related if r["id"] not in top_ids]
            logger.info("[LAW_SEARCH] server=%s q=%r → ЦЕЛЕВОЙ документ: %d статей (коды=%s, квалиф=%r)",
                        server_id, nq.original[:60], len(cards), nq.codes, nq.cleaned[:40])
            return _response(
                nq, cards, related, degraded=degraded,
                search_mode="partial" if degraded else "full", warnings=warnings,
            )
        if document_scope_ids:
            if diagnostics is not None:
                diagnostics["search_path"] = "code_doc_target_missing"
            logger.info(
                "[LAW_SEARCH] server=%s q=%r → документ распознан, code/kind в нём отсутствует "
                "(документы=%s, коды=%s, kind=%s)",
                server_id,
                nq.original[:60],
                document_scope_ids,
                nq.codes,
                requested_code_kind,
            )
            return _response(
                nq,
                degraded=degraded,
                search_mode="partial" if degraded else "full",
                warnings=warnings,
            )

    if diagnostics is not None:
        diagnostics["search_path"] = "hybrid"
    outcomes: Dict[str, _LegOutcome] = {
        name: _LegOutcome("skipped")
        for name in (
            "code", "explicit_code_title", "code_title_rescue",
            "title", "phrase_title",
            "legal_title", "intent_phrase", "synonym_exact",
            "synonym", "legal_fts", "fts", "trgm", "vector",
        )
    }
    outcomes["explicit_code_title"] = explicit_outcome
    tasks: Dict[str, Awaitable[_LegOutcome]] = {}
    if nq.codes:
        tasks["code"] = _run_sql_leg(
            "code",
            _leg_code(
                server_id,
                nq.codes,
                fallback_codes=nq.dash_fallback_codes,
                requested_kind=requested_code_kind,
            ),
        )
    if nq.weak_leading_code and nq.weak_title_remainder:
        tasks["code_title_rescue"] = _run_sql_leg(
            "code_title_rescue",
            _leg_code_title_rescue(
                server_id,
                nq.weak_leading_code,
                nq.weak_title_remainder,
            ),
        )
    if nq.cleaned:
        # Не планируем ноги, которые по своим length-guards неприменимы. Иначе
        # их мгновенный [] выглядел бы как healthy-empty и мог скрыть падение
        # всех действительно применимых ног на коротком запросе.
        tasks["fts"] = _run_scored_sql_leg(
            "fts", _leg_fts(server_id, nq.cleaned, nq.tokens)
        )
        if len(nq.cleaned) >= 2:
            tasks["title"] = _run_sql_leg("title", _leg_title(server_id, nq.cleaned))
            tasks["synonym"] = _run_scored_sql_leg(
                "synonym", _leg_synonyms(server_id, nq.cleaned)
            )
        if len(nq.cleaned) >= 3:
            tasks["trgm"] = _run_scored_sql_leg(
                "trgm", _leg_trgm(server_id, nq.cleaned)
            )
        if len(nq.cleaned) >= 4:
            tasks["synonym_exact"] = _run_sql_leg(
                "synonym_exact", _leg_synonym_exact(server_id, nq.cleaned)
            )
        if any(token in ("без", "не") for token in nq.tokens):
            tasks["phrase_title"] = _run_sql_leg(
                "phrase_title", _leg_phrase_title(server_id, nq.cleaned)
            )
    if nq.legal_variants:
        tasks["legal_fts"] = _run_sql_leg(
            "legal_fts", _leg_fts_variants(server_id, nq.legal_variants)
        )
        tasks["legal_title"] = _run_sql_leg(
            "legal_title", _leg_title_variants(server_id, nq.legal_variants)
        )
    if nq.intent_variants:
        tasks["intent_phrase"] = _run_sql_leg(
            "intent_phrase", _leg_intent_phrase(server_id, nq.intent_variants)
        )

    vector_requested = use_vector and bool(nq.cleaned)
    vector_text_kind = "not_requested"
    if vector_requested and config.LAW_CATALOG_EMBED_ENABLED:
        # Для подтверждённой ситуации используем нейтральную каноническую формулировку.
        # Остальные запросы сохраняют прежний production-контракт cleaned/original:
        # role-aware исправление не меняет FRIDA-ранжирование всего поиска разом.
        if nq.intent_vector_text:
            vector_text = nq.intent_vector_text
            vector_text_kind = "intent"
        elif config.LAW_VECTOR_QUERY_MODE == "cleaned":
            vector_text = nq.cleaned
            vector_text_kind = "cleaned"
        else:
            vector_text = nq.original
            vector_text_kind = "original"
        tasks["vector"] = _leg_vector_local(server_id, vector_text)
    elif vector_requested:
        vector_text_kind = "disabled"
        outcomes["vector"] = _LegOutcome(
            "skipped", warnings=["vector_disabled"], degraded=True, usable=False,
        )
    if diagnostics is not None:
        diagnostics["vector_requested"] = vector_requested
        diagnostics["vector_text_kind"] = vector_text_kind

    names = list(tasks)
    raw_results = await asyncio.gather(
        *(_timed_outcome(task) for task in tasks.values()),
        return_exceptions=True,
    )
    for name, res in zip(names, raw_results):
        if isinstance(res, Exception):
            logger.error("[LAW_SEARCH] необработанное падение ноги %s: %s: %s",
                         name, type(res).__name__, res)
            outcomes[name] = _LegOutcome("error", warnings=[f"{name}_error"], degraded=True)
        else:
            outcomes[name] = res

    if diagnostics is not None:
        leg_trace = diagnostics.setdefault("legs", {})
        leg_trace.update({
            name: _leg_diagnostics(outcome)
            for name, outcome in outcomes.items()
        })
        diagnostics["vector_usable"] = outcomes["vector"].usable

    # Пусто после хотя бы одной исправно отработавшей ноги — валидное «не найдено».
    # Пусто потому, что ВСЕ применимые ноги упали/истекли, — уже отказ сервиса, а не ответ.
    if names and not any(
        outcomes[name].status in ("ok", "empty")
        for name in names
    ):
        raise LawSearchUnavailable("all_legs_unavailable")

    for outcome in outcomes.values():
        warnings.extend(outcome.warnings)
        degraded = degraded or outcome.degraded
    legs = {name: outcome.ids for name, outcome in outcomes.items()}
    leg_confidences = {
        name: outcome.confidences
        for name, outcome in outcomes.items()
        if outcome.confidences
    }

    # Условный буст вектора: если ни одна точная нога (code/title/synonym_exact) не сработала —
    # запрос чисто перефразный, голосует по сути только вектор. Поднимаем его вес, иначе rank-1
    # вектора (1.0/61) задавливается рыхлым fts/synonym-соседом.
    weights = None
    weights_profile: List[str] = []
    exact_legs = tuple(
        leg
        for leg in _EXACT_LEGS
        if not (leg == "code" and nq.ambiguous_code_signal)
    )
    # Exact same-server ``code + complete title`` evidence is deliberately not
    # an authoritative/protected code hit, but a lone vector must not receive
    # the paraphrase boost against it.  Default vector weight still votes.
    vector_boost_blocked = bool(legs.get("code_title_rescue"))
    if (
        legs.get("vector")
        and not vector_boost_blocked
        and not any(legs.get(k) for k in exact_legs)
    ):
        weights = {**_LEG_WEIGHTS, "vector": config.LAW_VECTOR_BOOST_WEIGHT}
        weights_profile.append("vector_boost")
    if nq.ambiguous_code_signal:
        weights = dict(weights or _LEG_WEIGHTS)
        weights["code"] = _AMBIGUOUS_CODE_WEIGHT
        weights_profile.append("ambiguous_code")

    fused = _fuse(legs, weights, leg_confidences)
    if diagnostics is not None:
        diagnostics["weights_profile"] = "+".join(weights_profile) or "default"
        diagnostics["effective_weights"] = dict(weights or _LEG_WEIGHTS)
        diagnostics["rrf_candidates"] = [
            {
                "id": article_id,
                "score": round(score, 8),
                "sources": list(sources),
            }
            for article_id, score, sources in fused[:20]
        ]
    # Для однозначного ситуационного intent строгий full-text hit является ответом,
    # который владелец домена ожидает первым. Закрепляем его детерминированно: иначе
    # будущий шумный auto-synonym мог бы случайно собрать больше слабых RRF-голосов.
    priority_ids = [
        *legs.get("explicit_code_title", []),
        *legs.get("intent_phrase", []),
    ]
    if diagnostics is not None:
        diagnostics["priority_ids"] = list(priority_ids[:20])

    # RRF — retrieval, не последний приговор. Загружаем более широкий внутренний
    # pool, применяем только доказанные source-backed dominance rules и лишь затем
    # обрезаем до limit клиента. Иначе правильный кандидат на позиции 4/15 физически
    # недоступен deterministic reranker.
    candidate_fused = _prioritize_ids(fused, priority_ids)[:_RERANK_POOL_SIZE]
    candidate_ids = [aid for aid, _, _ in candidate_fused]
    cards_status, cards_by_id = await _cards_with_timeout(candidate_ids)
    if diagnostics is not None:
        diagnostics["cards_status"] = cards_status
        diagnostics["candidate_pool_size"] = len(candidate_ids)
    if cards_status in ("timeout", "error"):
        raise LawSearchUnavailable(f"cards_{cards_status}")
    if candidate_ids and not cards_by_id:
        raise LawSearchUnavailable("cards_unavailable")
    if (
        cards_status not in ("timeout", "error")
        and candidate_ids
        and len(cards_by_id) != len(candidate_ids)
    ):
        warnings.append("cards_incomplete")
        degraded = True

    candidate_fused = [
        item
        for item in candidate_fused
        if item[0] in cards_by_id
    ]
    protected_code_ids = (
        {
            article_id
            for article_id, _score, sources in candidate_fused
            if "code" in sources
        }
        if nq.codes and not nq.ambiguous_code_signal
        else set()
    )
    lexical_tokens = [
        token
        for token in nq.tokens
        if not (
            _CODE_RE.fullmatch(token)
            and token in nq.semantic_numbers
        )
    ]
    evidence_query = build_evidence_query(
        nq.original,
        " ".join(lexical_tokens).strip(),
        nq.evidence_facets,
    )
    evidence_status: LegStatus = "skipped"
    lexical_evidence: Dict[int, Dict[str, Any]] = {}
    if len(candidate_fused) > 1:
        evidence_status, lexical_evidence = await _ranking_evidence_with_timeout(
            server_id,
            [item[0] for item in candidate_fused],
            evidence_query,
        )
    lexical_rerank_applied = False
    lexical_rerank_skip_reason: Optional[str] = None
    polarity_sensitive = any(_polarity_flags(nq.cleaned))
    if evidence_status in ("timeout", "error"):
        warnings.append(f"evidence_{evidence_status}")
        degraded = True
    elif lexical_evidence:
        if polarity_sensitive:
            lexical_rerank_skip_reason = "polarity_sensitive_query"
        else:
            lexical_order = lexical_dominance_order(
                [item[0] for item in candidate_fused],
                lexical_evidence,
                protected_code_ids,
                {
                    item[0]: item[2]
                    for item in candidate_fused
                },
            )
            lexical_rerank_applied = lexical_order != [
                item[0] for item in candidate_fused
            ]
            by_article_id = {
                item[0]: item
                for item in candidate_fused
            }
            candidate_fused = [
                by_article_id[article_id]
                for article_id in lexical_order
                if article_id in by_article_id
            ]

    applicability_evidence: Dict[int, Dict[str, Any]] = {}
    applicability_rerank_applied = False
    applicability_rerank_skip_reason: Optional[str] = None
    if nq.semantic_numbers and not nq.applicability_query_ambiguous:
        applicability_evidence = evaluate_applicability(
            cards_by_id,
            numeric_query=nq.numeric_cleaned,
            semantic_numbers=nq.semantic_numbers,
        )
        if evidence_status == "ok" and not polarity_sensitive:
            applicability_order = _prioritize_applicability(
                candidate_fused,
                applicability_evidence,
                lexical_evidence,
                protected_code_ids,
            )
            applicability_rerank_applied = (
                applicability_order != candidate_fused
            )
            candidate_fused = applicability_order
    elif nq.semantic_numbers:
        applicability_rerank_skip_reason = "non_point_numeric_query"
    # Strict intent remains the final hard priority and cannot be displaced by
    # an unrelated numeric mention.
    candidate_fused = _prioritize_ids(candidate_fused, priority_ids)
    fused = candidate_fused[:max(1, min(limit, 20))]
    cards = [
        _card(cards_by_id[aid], score, sources)
        for aid, score, sources in fused
    ]
    if diagnostics is not None:
        diagnostics["evidence_status"] = evidence_status
        diagnostics["evidence_query"] = evidence_query
        diagnostics["lexical_rerank_applied"] = lexical_rerank_applied
        diagnostics["lexical_rerank_skip_reason"] = lexical_rerank_skip_reason
        diagnostics["applicability_rerank_applied"] = (
            applicability_rerank_applied
        )
        diagnostics["applicability_rerank_skip_reason"] = (
            applicability_rerank_skip_reason
        )
        diagnostics["lexical_evidence"] = {
            str(article_id): evidence
            for article_id, evidence in lexical_evidence.items()
        }
        diagnostics["applicability_evidence"] = {
            str(article_id): evidence
            for article_id, evidence in applicability_evidence.items()
        }
        diagnostics["reranked_candidates"] = [
            {
                "id": article_id,
                "score": round(score, 8),
                "sources": list(sources),
            }
            for article_id, score, sources in candidate_fused
        ]

    related: List[Dict[str, Any]] = []
    if cards:
        top_ids = {c["id"] for c in cards}
        related_status, raw_related = await _related_with_timeout(cards[0]["id"])
        if diagnostics is not None:
            diagnostics["related_status"] = related_status
        if related_status in ("timeout", "error", "skipped"):
            warnings.append(
                "related_rebuilding"
                if related_status == "skipped"
                else f"related_{related_status}"
            )
            degraded = True
        related = [r for r in raw_related if r["id"] not in top_ids]
    elif diagnostics is not None:
        diagnostics["related_status"] = "not_requested"

    vector_outcome = outcomes["vector"]
    search_mode = _mode(vector_requested, vector_outcome, degraded)

    logger.info(
        "[LAW_SEARCH] server=%s q=%r → cleaned=%r codes=%s: %d результатов "
        "(ноги: %s, status: %s, mode=%s)%s",
        server_id, nq.original[:60], nq.cleaned[:60], nq.codes, len(cards),
        {k: len(v) for k, v in legs.items()},
        {k: v.status for k, v in outcomes.items()}, search_mode,
        " [degraded]" if degraded else "",
    )
    return _response(
        nq, cards, related, degraded=degraded,
        search_mode=search_mode, warnings=warnings,
    )


async def search(
    server_id: str,
    query: str,
    limit: int = 8,
    use_vector: bool = True,
    diagnostics: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Гибридный поиск с общим серверным дедлайном; partial-ответ предпочтительнее HTTP 500."""
    nq = normalize_query(query)
    if diagnostics is not None:
        requested_code_kind = _requested_code_kind(nq)
        diagnostics.clear()
        diagnostics.update({
            "algorithm_version": LAW_SEARCH_ALGORITHM_VERSION,
            "embedding_space": config.LAW_CATALOG_EMBED_SPACE_ID,
            "intent_kind": nq.intent_kind,
            "query_normalized": nq.cleaned,
            "query_features": {
                "tokens": list(nq.tokens),
                "codes": list(nq.codes),
                "semantic_numbers": list(nq.semantic_numbers),
                "numeric_cleaned": nq.numeric_cleaned,
                "leading_numeric_literal": nq.leading_numeric_literal,
                "ambiguous_code_signal": nq.ambiguous_code_signal,
                "applicability_query_ambiguous": (
                    nq.applicability_query_ambiguous
                ),
                "weak_leading_code": nq.weak_leading_code,
                "weak_title_remainder": nq.weak_title_remainder,
                "evidence_facets": list(nq.evidence_facets),
                "code_signal_reason": _code_signal_reason(nq),
                "requested_code_kind": requested_code_kind,
                "document_scope_qualifier": (
                    _document_scope_qualifier(nq, requested_code_kind)
                    if _should_resolve_document_scope(nq, requested_code_kind)
                    else None
                ),
                "legal_variants": list(nq.legal_variants),
                "intent_variants": list(nq.intent_variants),
                "use_vector": bool(use_vector),
            },
        })
    try:
        return await asyncio.wait_for(
            _search_impl(
                server_id,
                nq,
                limit=limit,
                use_vector=use_vector,
                diagnostics=diagnostics,
            ),
            timeout=config.LAW_SEARCH_TOTAL_TIMEOUT,
        )
    except asyncio.TimeoutError:
        logger.error("[LAW_SEARCH] общий дедлайн %.2fs исчерпан: server=%s q=%r",
                     config.LAW_SEARCH_TOTAL_TIMEOUT, server_id, nq.original[:60])
        raise LawSearchUnavailable("search_timeout")


async def get_article(article_id: int) -> Optional[Dict[str, Any]]:
    """Полная статья для раскрытия карточки (+related)."""
    row = await _db_fetchrow(
        """
        SELECT id, law_document_id, server_id, article_ref, kind, code, title, doc_title,
               penalty_summary, penalty_json, full_text
        FROM law_articles WHERE id = $1
        """,
        article_id, timeout=config.LAW_SEARCH_CARDS_TIMEOUT,
    )
    if not row:
        return None
    art = dict(row)
    art["title"] = recover_rating_metadata_title(art.get("title"), art.get("full_text"))
    # Похожие статьи — необязательное украшение: их сбой не должен скрывать саму статью.
    _status, art["related"] = await _related_with_timeout(article_id)
    return art


async def servers_with_catalog() -> List[str]:
    """Серверы с построенным каталогом (для флага search в /servers)."""
    rows = await db_manager.fetch("SELECT DISTINCT server_id FROM law_articles")
    return [r["server_id"] for r in rows]

# END OF FILE services/law_search_service.py
