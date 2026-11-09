"""Постоянная матрица регрессий: рестрикторы клаузы.

Территория, которой не было ни в одной из восьми прежних матриц: часть
клаузы, ОГРАНИЧИВАЮЩАЯ утверждение.  Решающий орган («по решению
губернатора»), датив-ограничение круга лиц («лицам, достигшим
совершеннолетия»), эксклюзивное основание («исключительно на основании
ордера»), условие наличия («только при наличии ордера»), срок («сроком на
45 дней»).  Вывод, СБРОСИВШИЙ рестриктор, утверждает больше источника —
безусловное «лицензия аннулируется» против ограниченного «аннулируется по
решению губернатора» — и обязан отвергаться; вывод, ПОДМЕНИВШИЙ рестриктор
(«по решению мэра») или ВЫДУМАВШИЙ его на пустом источнике, — тем более.

Класс пришёл из разбора корня (docs/handoff/CODEX-V3-ROOT-VERDICT.md): судья
нашёл у проекта 1 класс дыр, не заявленный автором, — его канал принятия
пропускал сброс рестрикторов, и именно ОТСУТСТВИЕ таких клеток в постоянных
матрицах позволило дырявому каналу пройти восемь матриц незамеченным.  Шесть
атак судьи — auth-drop, auth-drop-b, restr-drop-dat, excl-basis-drop,
num-drop, only-ordr-drop — перенесены сюда дословно, источник и вывод байт в
байт (словарь ``JUDGE_CORE`` сверяется самопроверкой), и вокруг них
выстроен класс систематически: каждый вид рестриктора x (сброс / подмена /
дословное сохранение как контроль) x пять путей, плюс зеркала, где
рестриктор ВЫДУМАН выводом, и санкционные сиблинги, доказывающие живость
обеих penalty-ног на каждом виде рестриктора.

ЗАМЕР ДО ЛЕЧЕНИЯ (HEAD `11611f4`).  Ни одна из шести атак судьи на текущем
валидаторе дырой НЕ является: все шесть отвергаются на всех пяти путях, как
и остальные двадцать три атаки таблицы.  Дыр судья нашёл не у валидатора, а
у канала принятия проекта 1 — матрица фиксирует границу ДО того, как любой
новый канал принятия начнёт её двигать.  Держит атаки не семантика
рестриктора, а байтовый регистр: клейм ``strong_modal_family`` несёт
квалификатором дайджест ЦЕЛОЙ клаузы (сборка ``unresolved_finite_clause`` на
:15421 внутри ``_extract_qualitative_claims``, :50720); сброс или подмена
рестриктора меняет байты клаузы, дайджест расходится, и вывод режется как
source-unsupported semantic claim (:60863).  Замер JA02: у клаузы источника
дайджест оканчивается на 702dbc90…, у клаузы вывода — на 75a518bd…,
совпадения нет.  На penalty-ногах часть подмен ловится ещё и числовым гейтом
санкции (факты '14', '16', '30', '45', '90' отсутствуют в тексте санкции).
То есть у рестрикторного инварианта сегодня ЕДИНСТВЕННЫЙ владелец —
непрозрачный хеш клаузы, ровно тот механизм, который шаг 1 принятого плана
(«транспорт P3 + замки P2») собирается ослаблять.  Любое семантическое
устройство, заменяющее хеш, обязано удержать каждую клетку этой таблицы.

Три расхождения с доктриной, все — ложные блоки, все на дативных носителях
с глаголом «выдается»: дословный вывод проходит title/single-synonym/
full-set и режется обеими penalty-ногами.  Механизм — известный по
пассивной матрице дефект выходного аудита вхождений (:48656-48714): его
вердикт зависит не от вывода, а от ОСТАЛЬНОГО текста источника.  Доказано
прямой пробой: та же пара источник-вывод проходит single-synonym с голым
источником и режется, стоит источнику получить префикс «Наказание: »,
который penalty-ноги приписывают всегда.  Рестриктор ни при чём: носитель
«Пропуск выдается лицам.» без единого рестриктора режется теми же ногами
(MD01), а санкционный сиблинг того же вида рестриктора «Штраф назначается
лицам, достигшим совершеннолетия.» проходит все пять путей (SD01).

Устройство таблицы — по конвенции соседних постоянных матриц.  ``CASES`` —
кортежи (id, source, output, expected, measured, note); ``expected`` пишется
ТОЛЬКО по доктрине и выводится из имени случая МАШИННО: белые списки
суффиксов (``-literal`` — контроль, обязан быть непрерывной подстрокой
источника и пройти все пять путей; ``-drop``/``-swap``/``-invent`` — атака,
обязана подстрокой не быть и отвергнуться всеми пятью), имя вне списков
валит самопроверку.  Вид рестриктора зашит в имя атаки и сверяется по
ТЕКСТУ поверхностным маркером вида: у сброса маркер обязан исчезнуть из
вывода, у подмены — остаться, у выдумки — присутствовать в выводе при
источнике без маркера.  ``measured`` — реальный прогон; каждое расхождение
получает ``xfail(strict=True)`` с причиной и корнем, закрытие расхождения
даёт XPASS (заметим), регресс на сошедшейся клетке — обычное падение.
Ловушка предусловий penalty-ног снята контрольными тройками
``PENALTY_LIVENESS_TRIPLES``: на каждом виде рестриктора стоит санкционный
носитель, чей дословный вывод проходит все пять путей, а сброс и подмена
режутся, — значит, R на этих ногах — гейт, а не отказ предусловия.  Четыре
согласных R, стоящие в носителях с зарезанным дословным выводом, перечислены
поимённо в ``UNPROVEN_AGREEMENTS`` как растяжка.

Прибор проверен НА СЛОМ тремя атаками на копии файла: ослабление ожидания
(expected атаки переписан на PPPPP), переименование случая вне белого списка
суффиксов и удаление причины из ``XFAIL_REASONS`` — все три ловятся
самопроверкой либо сборкой параметров, отчёт лежит в журнале волны.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

import pytest

from services import law_enrichment_validation
from services.law_enrichment_validation import (
    validate_enrichment_title_source_evidence,
    validate_penalty_source_evidence,
    validate_synonym_source_evidence,
)


RelationValidator = Callable[[str, str], None]


_VALIDATOR_MODULE = "services/law_enrichment_validation.py"


# Обвязка penalty-путей: без явного признака санкции в ИСТОЧНИКЕ обе ноги
# падают на предусловии, поэтому источнику приписывается префикс
# «Наказание: » (та же конвенция, что в прежних постоянных матрицах).  Что
# предусловие действительно снято, доказывают дословные случаи (-literal),
# проходящие обе penalty-ноги, — по одному санкционному носителю на каждый
# вид рестриктора (``PENALTY_LIVENESS_TRIPLES``).
_PENALTY_SOURCE_PREFIX = "Наказание: "


def _validate_title(source: str, output: str) -> None:
    validate_enrichment_title_source_evidence(
        source,
        output,
        source_title=None,
    )


def _validate_single_synonym(source: str, output: str) -> None:
    validate_synonym_source_evidence(source, [output])


def _validate_full_set(source: str, output: str) -> None:
    source_surface = source.rstrip(".")
    validate_synonym_source_evidence(
        source,
        [source_surface, output]
        if output != source_surface
        else [source_surface],
    )


def _validate_penalty_summary(source: str, output: str) -> None:
    validate_penalty_source_evidence(
        _PENALTY_SOURCE_PREFIX + source,
        output,
        None,
    )


def _validate_penalty_other(source: str, output: str) -> None:
    validate_penalty_source_evidence(
        _PENALTY_SOURCE_PREFIX + source,
        None,
        {"other": output},
    )


ALL_OUTPUT_PATHS: tuple[RelationValidator, ...] = (
    _validate_title,
    _validate_single_synonym,
    _validate_full_set,
    _validate_penalty_summary,
    _validate_penalty_other,
)


PATH_IDS = (
    "title",
    "single-synonym",
    "full-set",
    "penalty-summary",
    "penalty-other",
)


# Носители.  Группы J* — источники батареи судьи байт в байт; S* —
# санкционные сиблинги того же вида рестриктора (живость penalty-ног);
# M* — зеркала: тот же носитель БЕЗ рестриктора.
SRC_JA = "Лицензия аннулируется по решению губернатора."
SRC_JB = "Пропуск аннулируется по решению руководителя."
SRC_SA = "Штраф налагается по решению руководителя."
SRC_MA = "Лицензия аннулируется."
SRC_JD = "Пропуск выдается лицам, достигшим совершеннолетия."
SRC_JG = "Пропуск выдается лицам, не имеющим судимости."
SRC_SD = "Штраф назначается лицам, достигшим совершеннолетия."
SRC_MD = "Пропуск выдается лицам."
SRC_JE = (
    "Обыск автомобиля может производиться исключительно на основании ордера."
)
SRC_SB = "Конфискация производится исключительно на основании решения суда."
SRC_MB = "Обыск автомобиля может производиться."
SRC_JO = "Обыск производится только при наличии ордера."
SRC_SO = "Арест назначается только при наличии ордера."
SRC_MO = "Обыск производится."
SRC_JN = "Конгрессмен избирается сроком на 45 дней."
SRC_SN = "Арест назначается сроком на 15 суток."
SRC_MN = "Конгрессмен избирается."


# Вид рестриктора каждой группы.  Атакующий суффикс имени обязан совпасть с
# видом своей группы, и вид сверяется по тексту поверхностным маркером.
GROUP_KIND: dict[str, str] = {
    "JA": "auth",
    "JB": "auth",
    "SA": "auth",
    "MA": "auth",
    "JD": "dat",
    "JG": "dat",
    "SD": "dat",
    "MD": "dat",
    "JE": "basis",
    "SB": "basis",
    "MB": "basis",
    "JO": "cond",
    "SO": "cond",
    "MO": "cond",
    "JN": "num",
    "SN": "num",
    "MN": "num",
}


# Поверхностный маркер вида: в источнике групп J*/S* он обязан быть, в
# источнике зеркал M* — отсутствовать; у сброса он обязан исчезнуть из
# вывода, у подмены — остаться, у выдумки — присутствовать в выводе.
KIND_MARKERS: dict[str, str] = {
    "auth": "по решению",
    "dat": "лицам, ",
    "basis": "исключительно на основании",
    "cond": "только при наличии",
    "num": "сроком на ",
}


_ID_RE = re.compile(
    r"^(?P<prefix>[JSM][A-Z])\d{2}-"
    r"(?:literal|(?P<kind>auth|dat|basis|cond|num)-(?P<move>drop|swap|invent))$"
)


# (id, source, output, expected, measured, note)
# expected/measured — ровно пять букв P/R в порядке PATH_IDS.  Доктрина
# выводится из имени механически: -literal обязан быть непрерывной
# подстрокой источника и пройти (PPPPP), всякая атака — не быть и
# отвергнуться (RRRRR).  Самопроверка внизу файла проверяет обе половины по
# самому тексту, а не по вере в имя.
CASES: tuple[tuple[str, str, str, str, str, str], ...] = (
    # --- auth: решающий орган «по решению X» -----------------------------
    (
        "JA01-literal",
        SRC_JA,
        "Лицензия аннулируется по решению губернатора",
        "PPPPP",
        "PPPPP",
        "Контроль носителя ядра: дословная клауза с рестриктором решающего "
        "органа проходит все пять путей — обе penalty-ноги доходят до "
        "разбора, предусловие санкции снято.",
    ),
    (
        "JA02-auth-drop",
        SRC_JA,
        "лицензия аннулируется",
        "RRRRR",
        "RRRRR",
        "Ядро судьи (auth-drop): сброшен решающий орган, безусловное "
        "«аннулируется» утверждает больше источника.  Держит байтовый "
        "регистр: дайджест клаузы вывода не совпадает с дайджестом клаузы "
        "источника, клейм strong_modal_family режется как неподтверждённый.",
    ),
    (
        "JA03-auth-swap",
        SRC_JA,
        "лицензия аннулируется по решению мэра",
        "RRRRR",
        "RRRRR",
        "Подмена решающего органа (губернатор на мэра): орган выдуман, "
        "режется тем же регистром.",
    ),
    (
        "JB01-literal",
        SRC_JB,
        "Пропуск аннулируется по решению руководителя",
        "PPPPP",
        "PPPPP",
        "Контроль второго носителя ядра: дословная клауза проходит все "
        "пять путей.",
    ),
    (
        "JB02-auth-drop",
        SRC_JB,
        "пропуск аннулируется",
        "RRRRR",
        "RRRRR",
        "Ядро судьи (auth-drop-b): тот же сброс решающего органа на втором "
        "носителе — класс воспроизводится, не единичный случай.",
    ),
    (
        "JB03-auth-swap",
        SRC_JB,
        "пропуск аннулируется по решению заместителя",
        "RRRRR",
        "RRRRR",
        "Подмена руководителя заместителем: выдуманный орган режется "
        "регистром.",
    ),
    (
        "SA01-literal",
        SRC_SA,
        "Штраф налагается по решению руководителя",
        "PPPPP",
        "PPPPP",
        "Санкционный носитель вида auth: дословный вывод проходит все пять "
        "путей — контрольная тройка живости penalty-ног.",
    ),
    (
        "SA02-auth-drop",
        SRC_SA,
        "штраф налагается",
        "RRRRR",
        "RRRRR",
        "Сброс «по решению руководителя» на доказанно живом носителе: R на "
        "penalty-ногах — настоящий гейт, а не отказ предусловия.",
    ),
    (
        "SA03-auth-swap",
        SRC_SA,
        "штраф налагается по решению комиссии",
        "RRRRR",
        "RRRRR",
        "Подмена руководителя комиссией на живом носителе: режется всеми "
        "пятью путями.",
    ),
    (
        "MA01-literal",
        SRC_MA,
        "Лицензия аннулируется",
        "PPPPP",
        "PPPPP",
        "Контроль зеркала: источник без рестриктора, дословный вывод "
        "проходит все пять путей.",
    ),
    (
        "MA02-auth-invent",
        SRC_MA,
        "лицензия аннулируется по решению губернатора",
        "RRRRR",
        "RRRRR",
        "Зеркало: решающий орган ВЫДУМАН выводом — у источника нет «по "
        "решению губернатора»; режется клеймом instrument_revocation "
        "(authority вывода источником не подтверждён).",
    ),
    # --- dat: датив-ограничение круга лиц --------------------------------
    (
        "JD01-literal",
        SRC_JD,
        "Пропуск выдается лицам, достигшим совершеннолетия",
        "PPPPP",
        "PPPRR",
        "ЛОЖНЫЙ БЛОК: дословный датив проходит первые три пути и режется "
        "обеими penalty-ногами — вердикт аудита вхождений зависит от "
        "префикса обвязки, а не от вывода.",
    ),
    (
        "JD02-dat-drop",
        SRC_JD,
        "пропуск выдается лицам",
        "RRRRR",
        "RRRRR",
        "Ядро судьи (restr-drop-dat): сброшено причастное ограничение — "
        "«лицам» вместо «лицам, достигшим совершеннолетия» расширяет круг "
        "получателей.  Первые три пути держит регистр; R на penalty-ногах "
        "измерен гейтом клеймов, но носитель не доказан дословным PPPPP — "
        "запись идёт растяжкой.",
    ),
    (
        "JD03-dat-swap",
        SRC_JD,
        "пропуск выдается лицам, достигшим четырнадцати лет",
        "RRRRR",
        "RRRRR",
        "Подмена возраста (совершеннолетие на четырнадцать лет): первые "
        "три пути держит регистр, penalty-ноги ловят числовой факт '14', "
        "отсутствующий в санкции; носитель не доказан — растяжка.",
    ),
    (
        "JG01-literal",
        SRC_JG,
        "Пропуск выдается лицам, не имеющим судимости",
        "PPPPP",
        "PPPRR",
        "ЛОЖНЫЙ БЛОК: тот же аудит режет дословное отрицательное причастие "
        "на обеих penalty-ногах.",
    ),
    (
        "JG02-dat-drop",
        SRC_JG,
        "пропуск выдается лицам",
        "RRRRR",
        "RRRRR",
        "Сброс «не имеющим судимости»: снятие отрицательного ограничения "
        "расширяет круг лиц; носитель не доказан — растяжка на "
        "penalty-ногах.",
    ),
    (
        "SD01-literal",
        SRC_SD,
        "Штраф назначается лицам, достигшим совершеннолетия",
        "PPPPP",
        "PPPPP",
        "Санкционный датив вида dat: дословный вывод проходит все пять "
        "путей — тройка живости; тот же вид рестриктора, что у зарезанного "
        "«выдается», но глагол «назначается» аудит проходит.",
    ),
    (
        "SD02-dat-drop",
        SRC_SD,
        "штраф назначается лицам",
        "RRRRR",
        "RRRRR",
        "Сброс датив-ограничения на живом носителе: настоящий гейт на всех "
        "пяти путях.",
    ),
    (
        "SD03-dat-swap",
        SRC_SD,
        "штраф назначается лицам, достигшим шестнадцати лет",
        "RRRRR",
        "RRRRR",
        "Подмена возраста на живом носителе: первые три пути держит "
        "регистр, penalty-ноги ловят числовой факт '16'.",
    ),
    (
        "MD01-literal",
        SRC_MD,
        "Пропуск выдается лицам",
        "PPPPP",
        "PPPRR",
        "ЛОЖНЫЙ БЛОК: «выдается» режется penalty-ногами даже БЕЗ "
        "рестриктора — блок сидит в глаголе и префиксе обвязки, рестриктор "
        "ни при чём.",
    ),
    (
        "MD02-dat-invent",
        SRC_MD,
        "пропуск выдается лицам, достигшим совершеннолетия",
        "RRRRR",
        "RRRRR",
        "Зеркало: датив-ограничение ВЫДУМАНО выводом — источник даёт "
        "пропуск лицам без условия возраста; носитель не доказан — "
        "растяжка на penalty-ногах.",
    ),
    # --- basis: эксклюзивное основание -----------------------------------
    (
        "JE01-literal",
        SRC_JE,
        "Обыск автомобиля может производиться исключительно на основании "
        "ордера",
        "PPPPP",
        "PPPPP",
        "Контроль носителя ядра: дословная клауза с «исключительно на "
        "основании» проходит все пять путей.",
    ),
    (
        "JE02-basis-drop",
        SRC_JE,
        "автомобиль можно обыскать",
        "RRRRR",
        "RRRRR",
        "Ядро судьи (excl-basis-drop): сброс эксклюзивного основания "
        "вместе с лексической перестройкой («можно обыскать»); режется "
        "модальным клеймом «обыск», неподтверждённым источником.",
    ),
    (
        "JE03-basis-drop",
        SRC_JE,
        "обыск автомобиля может производиться",
        "RRRRR",
        "RRRRR",
        "Чистый сброс «исключительно на основании ордера» без "
        "перестройки: обыск становится безусловным; держит регистр — "
        "дайджест клаузы расходится.",
    ),
    (
        "JE04-basis-swap",
        SRC_JE,
        "обыск автомобиля может производиться исключительно на основании "
        "постановления",
        "RRRRR",
        "RRRRR",
        "Подмена основания (ордер на постановление): документ выдуман, "
        "режется регистром.",
    ),
    (
        "SB01-literal",
        SRC_SB,
        "Конфискация производится исключительно на основании решения суда",
        "PPPPP",
        "PPPPP",
        "Санкционный носитель вида basis (конфискация): дословный вывод "
        "проходит все пять путей — тройка живости.",
    ),
    (
        "SB02-basis-drop",
        SRC_SB,
        "конфискация производится",
        "RRRRR",
        "RRRRR",
        "Сброс эксклюзивного основания на живом носителе: настоящий гейт "
        "на всех пяти путях.",
    ),
    (
        "SB03-basis-swap",
        SRC_SB,
        "конфискация производится исключительно на основании приказа",
        "RRRRR",
        "RRRRR",
        "Подмена решения суда приказом на живом носителе: режется всеми "
        "пятью путями.",
    ),
    (
        "MB01-literal",
        SRC_MB,
        "Обыск автомобиля может производиться",
        "PPPPP",
        "PPPPP",
        "Контроль зеркала: источник без основания, дословный вывод "
        "проходит.",
    ),
    (
        "MB02-basis-invent",
        SRC_MB,
        "обыск автомобиля может производиться исключительно на основании "
        "ордера",
        "RRRRR",
        "RRRRR",
        "Зеркало: эксклюзивное основание ВЫДУМАНО выводом — ордера в "
        "источнике нет.",
    ),
    # --- cond: условие наличия -------------------------------------------
    (
        "JO01-literal",
        SRC_JO,
        "Обыск производится только при наличии ордера",
        "PPPPP",
        "PPPPP",
        "Контроль носителя ядра: дословная клауза с «только при наличии» "
        "проходит все пять путей.",
    ),
    (
        "JO02-cond-drop",
        SRC_JO,
        "обыск производится",
        "RRRRR",
        "RRRRR",
        "Ядро судьи (only-ordr-drop): сброс условия наличия ордера даёт "
        "безусловный обыск; держит регистр.",
    ),
    (
        "JO03-cond-swap",
        SRC_JO,
        "обыск производится только при наличии постановления",
        "RRRRR",
        "RRRRR",
        "Подмена ордера постановлением: документ условия выдуман, режется "
        "регистром.",
    ),
    (
        "SO01-literal",
        SRC_SO,
        "Арест назначается только при наличии ордера",
        "PPPPP",
        "PPPPP",
        "Санкционный носитель вида cond (арест): дословный вывод проходит "
        "все пять путей — тройка живости.",
    ),
    (
        "SO02-cond-drop",
        SRC_SO,
        "арест назначается",
        "RRRRR",
        "RRRRR",
        "Сброс условия на живом носителе: настоящий гейт на всех пяти "
        "путях.",
    ),
    (
        "SO03-cond-swap",
        SRC_SO,
        "арест назначается только при наличии постановления",
        "RRRRR",
        "RRRRR",
        "Подмена документа условия на живом носителе: режется всеми пятью "
        "путями.",
    ),
    (
        "MO01-literal",
        SRC_MO,
        "Обыск производится",
        "PPPPP",
        "PPPPP",
        "Контроль зеркала: источник без условия, дословный вывод проходит.",
    ),
    (
        "MO02-cond-invent",
        SRC_MO,
        "обыск производится только при наличии ордера",
        "RRRRR",
        "RRRRR",
        "Зеркало: условие наличия ордера ВЫДУМАНО выводом.",
    ),
    # --- num: срок --------------------------------------------------------
    (
        "JN01-literal",
        SRC_JN,
        "Конгрессмен избирается сроком на 45 дней",
        "PPPPP",
        "PPPPP",
        "Контроль носителя ядра: дословная клауза со сроком проходит все "
        "пять путей.",
    ),
    (
        "JN02-num-drop",
        SRC_JN,
        "конгрессмен избирается",
        "RRRRR",
        "RRRRR",
        "Ядро судьи (num-drop): сброшен срок «сроком на 45 дней» — "
        "бессрочное избрание утверждает больше источника; держит регистр.",
    ),
    (
        "JN03-num-swap",
        SRC_JN,
        "конгрессмен избирается сроком на 90 дней",
        "RRRRR",
        "RRRRR",
        "Подмена срока (45 на 90 дней): первые три пути держит регистр, "
        "penalty-ноги ловят числовой факт '90', отсутствующий в санкции.",
    ),
    (
        "SN01-literal",
        SRC_SN,
        "Арест назначается сроком на 15 суток",
        "PPPPP",
        "PPPPP",
        "Санкционный носитель вида num — формулировка санкции в духе КоАП: "
        "дословный вывод проходит все пять путей — тройка живости.",
    ),
    (
        "SN02-num-drop",
        SRC_SN,
        "арест назначается",
        "RRRRR",
        "RRRRR",
        "Сброс срока на живом носителе: настоящий гейт на всех пяти путях.",
    ),
    (
        "SN03-num-swap",
        SRC_SN,
        "арест назначается сроком на 30 суток",
        "RRRRR",
        "RRRRR",
        "Подмена срока (15 на 30 суток) на живом носителе: первые три пути "
        "держит регистр, penalty-ноги ловят числовой факт '30'.",
    ),
    (
        "MN01-literal",
        SRC_MN,
        "Конгрессмен избирается",
        "PPPPP",
        "PPPPP",
        "Контроль зеркала: источник без срока, дословный вывод проходит.",
    ),
    (
        "MN02-num-invent",
        SRC_MN,
        "конгрессмен избирается сроком на 45 дней",
        "RRRRR",
        "RRRRR",
        "Зеркало: срок ВЫДУМАН выводом — «45 дней» в источнике нет; "
        "penalty-ноги ловят и числовой факт '45'.",
    ),
)


# Шесть атак судьи из батареи разбора корня (jbattery.py / jtopic.py):
# имя атаки судьи -> (id случая, источник, вывод) байт в байт.  Самопроверка
# требует совпадения пары с таблицей и вердикта RRRRR: на текущем валидаторе
# ни одна из шести не дыра, и любой будущий канал принятия, открывший хоть
# одну, уронит матрицу обычным FAILED.
JUDGE_CORE: dict[str, tuple[str, str, str]] = {
    "auth-drop": ("JA02-auth-drop", SRC_JA, "лицензия аннулируется"),
    "auth-drop-b": ("JB02-auth-drop", SRC_JB, "пропуск аннулируется"),
    "restr-drop-dat": ("JD02-dat-drop", SRC_JD, "пропуск выдается лицам"),
    "excl-basis-drop": (
        "JE02-basis-drop",
        SRC_JE,
        "автомобиль можно обыскать",
    ),
    "num-drop": ("JN02-num-drop", SRC_JN, "конгрессмен избирается"),
    "only-ordr-drop": ("JO02-cond-drop", SRC_JO, "обыск производится"),
}


# Ссылки на корень проверяются машинно: ROOT_ANCHORS ниже требует, чтобы
# каждая упомянутая строка валидатора содержала свой якорь в ЖИВОМ файле.
_BLOCK_TAIL = (
    "выходной аудит вхождений не признаёт когерентным ни одно чтение "
    "дословной клаузы с «выдается», и его вердикт зависит не от вывода, а "
    "от ОСТАЛЬНОГО текста источника: та же пара проходит single-synonym с "
    "голым источником и режется, стоит источнику получить префикс "
    "«Наказание: », который обе penalty-ноги приписывают всегда, корень "
    f"{_VALIDATOR_MODULE}:48656-48714"
)


XFAIL_REASONS: dict[str, str] = {
    "JD01-literal": (
        "ЛОЖНЫЙ БЛОК: дословный датив с причастным ограничением проходит "
        "title/single-synonym/full-set и режется обеими penalty-ногами — "
        + _BLOCK_TAIL
    ),
    "JG01-literal": (
        "ЛОЖНЫЙ БЛОК: то же на отрицательном причастии («не имеющим "
        "судимости») — " + _BLOCK_TAIL
    ),
    "MD01-literal": (
        "ЛОЖНЫЙ БЛОК: то же даже без единого рестриктора — режет сам "
        "глагол «выдается» на penalty-ногах, рестриктор ни при чём — "
        + _BLOCK_TAIL
    ),
}


# Носители, в которых НИ ОДИН дословный вывод не проходит все пять путей:
# R на нечестных выводах этих носителей нельзя отличить от отказа
# предусловия penalty-ног, и такие согласия перечислены поимённо ниже.
CARRIERS_WITH_BLOCKED_LITERAL: frozenset[str] = frozenset((
    SRC_JD,
    SRC_JG,
    SRC_MD,
))


# Случаи, чей вердикт R засчитан как согласие с доктриной, хотя стоят они в
# недоказанном носителе.  Замер показал у всех четырёх на penalty-ногах
# сообщение гейта клеймов, а не аудита, — но по стандарту тройки носитель не
# доказан, поэтому запись идёт растяжкой, как в пассивной матрице.
UNPROVEN_AGREEMENTS: frozenset[str] = frozenset((
    "JD02-dat-drop",
    "JD03-dat-swap",
    "JG02-dat-drop",
    "MD02-dat-invent",
))


# Контрольные тройки живости penalty-ног: на каждом виде рестриктора стоит
# санкционный носитель, чей дословный вывод проходит все пять путей (обе
# penalty-ноги дошли до разбора), а сброс и подмена режутся всеми пятью.
PENALTY_LIVENESS_TRIPLES: tuple[tuple[str, str, str], ...] = (
    ("SA01-literal", "SA02-auth-drop", "SA03-auth-swap"),
    ("SD01-literal", "SD02-dat-drop", "SD03-dat-swap"),
    ("SB01-literal", "SB02-basis-drop", "SB03-basis-swap"),
    ("SO01-literal", "SO02-cond-drop", "SO03-cond-swap"),
    ("SN01-literal", "SN02-num-drop", "SN03-num-swap"),
)


# Якоря корня: каждая строка валидатора, названная в этом файле, обязана
# содержать свой якорь в ЖИВОМ файле — ссылка, которую никто не сверяет,
# это украшение (урок пассивной матрицы).
ROOT_ANCHORS: dict[int, str] = {
    15421: '"unresolved_finite_clause",',
    48656: "def _validate_relation_occurrence_grammar(",
    48714: "no coherent grammatical actor/patient",
    50720: "def _extract_qualitative_claims(",
    60863: "source-unsupported semantic claim",
}


def _matrix_params() -> tuple[object, ...]:
    params: list[object] = []
    for case_id, source, output, expected, measured, _note in CASES:
        for index, path_id in enumerate(PATH_IDS):
            validate = ALL_OUTPUT_PATHS[index]
            expects_raise = expected[index] == "R"
            param_id = f"{case_id}-{path_id}"
            marks: tuple[object, ...] = ()
            if measured[index] != expected[index]:
                marks = (
                    pytest.mark.xfail(
                        strict=True,
                        reason=XFAIL_REASONS[case_id],
                    ),
                )
            params.append(
                pytest.param(
                    source,
                    output,
                    expects_raise,
                    validate,
                    id=param_id,
                    marks=marks,
                )
            )
    return tuple(params)


@pytest.mark.parametrize(
    ("source", "output", "expects_raise", "validate"),
    _matrix_params(),
)
def test_v3_restrictor_permanent_matrix(
    source: str,
    output: str,
    expects_raise: bool,
    validate: RelationValidator,
) -> None:
    if expects_raise:
        with pytest.raises(ValueError):
            validate(source, output)
        return
    validate(source, output)


def test_v3_restrictor_matrix_bookkeeping() -> None:
    """Прибор сторожит сам себя: доктрину, имена, счётчики и корни.

    1. колонка ``expected`` выводится из имени МЕХАНИЧЕСКИ по белым спискам
       суффиксов, а само имя сверяется по тексту: «-literal» обязан быть
       непрерывной подстрокой источника, атака — не быть; имя вне списков
       валит регэксп;
    2. вид рестриктора зашит в имя атаки, обязан совпасть с видом группы и
       сверяется ПОВЕРХНОСТНЫМ МАРКЕРОМ: у сброса маркер исчезает из вывода,
       у подмены — остаётся, у выдумки — присутствует при источнике без
       маркера; у зеркал маркера в источнике нет, у остальных — есть;
    3. порядок колонок привязан к именам функций-путей;
    4. пояснение следует замеру: «ДЫРА» допустима только там, где течёт,
       «ЛОЖНЫЙ БЛОК» — только там, где режет;
    5. словарь причин совпадает с множеством расходящихся случаев, счётчики
       пришпилены: дыр ноль, ложных блоков три, расходящихся пар шесть;
    6. шесть атак судьи стоят в таблице байт в байт и все отвергнуты;
    7. живость обеих penalty-ног доказана тройкой на КАЖДОМ виде
       рестриктора, согласия в недоказанных носителях перечислены поимённо;
    8. каждая координата корня сверяется с живым файлом валидатора.
    """

    assert len(ALL_OUTPUT_PATHS) == len(PATH_IDS) == 5
    for path_id, validate in zip(PATH_IDS, ALL_OUTPUT_PATHS):
        assert validate.__name__ == "_validate_" + path_id.replace("-", "_")

    ids = [case[0] for case in CASES]
    assert len(set(ids)) == len(ids) == 46

    assert set(GROUP_KIND.values()) == set(KIND_MARKERS)

    holes: list[str] = []
    false_blocks: list[str] = []
    divergent: list[str] = []
    literal_by_source: dict[str, list[str]] = {}
    moves_by_kind: dict[str, set[str]] = {}
    literal_kinds_plain: set[str] = set()
    literal_kinds_mirror: set[str] = set()
    ledger = {case[0]: case[4] for case in CASES}

    for case_id, source, output, expected, measured, note in CASES:
        assert len(expected) == len(measured) == 5, case_id
        assert set(expected) <= {"P", "R"}, case_id
        assert set(measured) <= {"P", "R"}, case_id
        assert source.endswith("."), case_id
        assert note.strip(), case_id

        # (1) имя по белому списку, доктрина из имени, имя по тексту.
        match = _ID_RE.fullmatch(case_id)
        assert match is not None, case_id
        prefix = match.group("prefix")
        assert prefix in GROUP_KIND, case_id
        kind = GROUP_KIND[prefix]
        mirror = prefix.startswith("M")
        marker = KIND_MARKERS[kind]

        verbatim = case_id.endswith("-literal")
        assert (output in source) is verbatim, case_id
        assert expected == ("PPPPP" if verbatim else "RRRRR"), case_id
        literal_by_source.setdefault(source, [])
        if verbatim:
            literal_by_source[source].append(case_id)
            (literal_kinds_mirror if mirror else literal_kinds_plain).add(
                kind
            )

        # (2) вид рестриктора сверяется по тексту поверхностным маркером.
        assert (marker in source) is (not mirror), case_id
        move = match.group("move")
        if move is not None:
            assert match.group("kind") == kind, case_id
            moves_by_kind.setdefault(kind, set()).add(move)
            if move == "drop":
                assert not mirror, case_id
                assert marker not in output, case_id
            elif move == "swap":
                assert not mirror, case_id
                assert marker in output, case_id
            else:
                assert move == "invent", case_id
                assert mirror, case_id
                assert marker in output, case_id

        pairs = [
            index for index in range(5) if expected[index] != measured[index]
        ]
        leaks = [index for index in pairs if measured[index] == "P"]
        blocks = [index for index in pairs if measured[index] == "R"]
        assert not (leaks and blocks), case_id

        # (4) пояснение обязано следовать замеру.
        assert ("ДЫРА" in note) is bool(leaks), case_id
        assert ("ЛОЖНЫЙ БЛОК" in note) is bool(blocks), case_id

        if not pairs:
            assert case_id not in XFAIL_REASONS, case_id
            continue
        divergent.extend(f"{case_id}-{PATH_IDS[index]}" for index in pairs)
        assert case_id in XFAIL_REASONS, case_id
        reason = XFAIL_REASONS[case_id]
        assert reason.startswith(
            "ДЫРА: " if leaks else "ЛОЖНЫЙ БЛОК: "
        ), case_id
        assert _VALIDATOR_MODULE in reason, case_id
        (holes if leaks else false_blocks).append(case_id)

    # (5) словарь причин == множество расходящихся; счётчики пришпилены.
    assert set(XFAIL_REASONS) == set(holes) | set(false_blocks)
    assert holes == []
    assert sorted(false_blocks) == [
        "JD01-literal",
        "JG01-literal",
        "MD01-literal",
    ]
    assert len(divergent) == 6
    for case_id in false_blocks:
        assert ledger[case_id] == "PPPRR", case_id
        assert "penalty" in XFAIL_REASONS[case_id], case_id

    # У каждого носителя есть дословный случай.
    for source, literals in literal_by_source.items():
        assert literals, source

    # Полнота класса: каждый вид рестриктора несёт сброс, подмену и
    # выдумку, дословный контроль стоит и в обычной группе, и в зеркале.
    assert set(moves_by_kind) == set(KIND_MARKERS)
    for kind, moves in moves_by_kind.items():
        assert moves == {"drop", "swap", "invent"}, kind
    assert literal_kinds_plain == literal_kinds_mirror == set(KIND_MARKERS)

    # (6) шесть атак судьи: пары байт в байт, вердикт RRRRR у каждой.
    assert len(JUDGE_CORE) == 6
    by_id = {case[0]: case for case in CASES}
    for attack_name, (case_id, source, output) in JUDGE_CORE.items():
        case = by_id[case_id]
        assert case[1] == source, attack_name
        assert case[2] == output, attack_name
        assert case[4] == "RRRRR", attack_name

    # (7) живость penalty-ног: тройка на каждом виде рестриктора.
    assert {
        GROUP_KIND[triple[0][:2]] for triple in PENALTY_LIVENESS_TRIPLES
    } == set(KIND_MARKERS)
    for literal_id, drop_id, swap_id in PENALTY_LIVENESS_TRIPLES:
        triple = {literal_id, drop_id, swap_id}
        assert len({case[1] for case in CASES if case[0] in triple}) == 1
        assert ledger[literal_id] == "PPPPP", literal_id
        assert ledger[drop_id] == "RRRRR", drop_id
        assert ledger[swap_id] == "RRRRR", swap_id

    # Согласия в недоказанных носителях перечислены поимённо.
    live_sources = {
        case[1]
        for case in CASES
        if case[0].endswith("-literal") and case[4] == "PPPPP"
    }
    unproven_agreements = {
        case[0]
        for case in CASES
        if not case[0].endswith("-literal")
        and case[3] == case[4] == "RRRRR"
        and case[1] not in live_sources
    }
    assert unproven_agreements == set(UNPROVEN_AGREEMENTS)
    assert {
        case[1] for case in CASES if case[0] in unproven_agreements
    } == set(CARRIERS_WITH_BLOCKED_LITERAL)

    # (8) ссылка на корень сверяется с ЖИВЫМ файлом валидатора.
    validator_path = Path(law_enrichment_validation.__file__)
    assert validator_path.name == Path(_VALIDATOR_MODULE).name
    last_anchor = max(ROOT_ANCHORS)
    anchored: dict[int, str] = {}
    with validator_path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            if number in ROOT_ANCHORS:
                anchored[number] = line
            if number >= last_anchor:
                break
    for number, anchor in ROOT_ANCHORS.items():
        assert anchor in anchored[number], (number, anchor)
    module_doc = __doc__ or ""
    assert module_doc.strip(), "модульная докстрока пуста"
    cited = {
        int(number)
        for text in (module_doc, *XFAIL_REASONS.values())
        for number in re.findall(r"[:-](\d{4,6})\b", text)
    }
    assert cited == set(ROOT_ANCHORS), cited ^ set(ROOT_ANCHORS)
