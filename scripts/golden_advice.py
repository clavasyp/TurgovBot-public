# START OF FILE scripts/golden_advice.py

"""
Golden-set харнесс качества режима «Совет» оверлея (этап 0 плана docs/OVERLAY_ADVICE_PROMPT_PLAN.md).

Запускается ВНУТРИ прод-контейнера api/bot (образ содержит исходники, creds Vertex и доступ к БД):

    docker compose exec api python -m scripts.golden_advice --mode record
    docker compose exec api python -m scripts.golden_advice --mode prompt-ab
    docker compose exec api python -m scripts.golden_advice --mode pipeline-ab

Режимы:
  record      — живой retrieve() по каждому кейсу, чанки сохраняются в фикстуру (--out);
                заодно считается retrieval-recall: попали ли ожидаемые статьи в выдачу.
                Как и в боевом пути, история кейса в ретривале НЕ участвует (факт A1).
  prompt-ab   — слепое A/B промптов на ОДИНАКОВЫХ чанках из фикстуры (--fixtures):
                services.advice_service.retrieve (и retrieve_multi/rewrite_queries, если уже
                вживлены) монкипатчатся, каждый кейс гоняется вариантами old (боевой промпт
                ДО v3 — константа OLD_PROMPT, HEAD b616f2f; воспроизводимый baseline после
                деплоя v3), current (текущий модульный ADVICE_SYSTEM_PROMPT) и v3 (константа
                V3_PROMPT ниже — вербатим из docs/OVERLAY_ADVICE_PROMPTS_V3.md §1).
                БД для этого режима не нужна.
  pipeline-ab — живой конвейер: текущий путь (риврайтер принудительно выключен) против
                нового (rewrite_queries + retrieve_multi), если этап 3 уже вживлён в
                advice_service; иначе — понятное сообщение и только живой baseline.

Отчёт — markdown (--report): по каждому кейс×вариант упомянутые статьи
(advice_service._ARTICLE_NUM_RE), hit/miss по expected_refs, фантомные номера
(вне поданных норм) и полный текст ответа; в конце — сводная таблица hit-rate.

QPM-аккуратность (preview-квоты Vertex!): все вызовы строго последовательные,
пауза между генерациями --pause (дефолт 2.5 с), максимум 1 ретрай НА УРОВНЕ СКРИПТА.
⚠️ Внутри generate_advice свои вызовы: execute_vertex_request(max_retries=2),
enforcement-повтор при фантомах (+1 вызов), а в pipeline-ab/live-rewrite ещё риврайтер —
реальный расход на «кейс×вариант» в 2-3 раза выше «одного вызова»; на узкой
preview-квоте поднимай --pause до 5-6 с.

Вне контейнера скрипт честно падает с читаемым сообщением (нет creds/БД).
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

# --- ADVICE_SYSTEM_PROMPT v3 — ВЕРБАТИМ из docs/OVERLAY_ADVICE_PROMPTS_V3.md §1 (не редактировать!) ---
V3_PROMPT = """Ты — голосовой юридический ассистент игрока на RP-сервере (штат Сан-Андреас, GTA5RP).
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


# --- ADVICE_SYSTEM_PROMPT из HEAD b616f2f (боевой ДО v3) — вариант prompt-ab «old»:
# воспроизводимый baseline-замер ПОСЛЕ деплоя v3 (этап 0 плана), без git checkout. Вербатим! ---
OLD_PROMPT = """Ты — голосовой ассистент по ВНУТРИИГРОВОМУ праву RP-сервера.
Игрок в реальном времени попадает в ситуацию (задержание, проверка, суд, конфликт с \
сотрудником) и ему нужен мгновенный короткий совет, который читается за пару секунд на оверлее.

Тебе дают РЕЛЕВАНТНЫЕ НОРМЫ сервера (фрагменты с номерами статей) и описание ситуации/вопрос.

Правила ответа (СТРОГО соблюдай порядок и формат):
- ПЕРВОЙ СТРОКОЙ — всегда готовая реплика, которую игрок СРАЗУ произносит вслух, в формате:
  Скажи: «…».
  Короткая естественная разговорная речь от лица игрока (адвоката/прокурора/участника), по сути ситуации, с опорой на нормы ниже. Это ОБЯЗАТЕЛЬНО, а не опционально.
- Затем 1–3 пункта обоснования, каждый — одна короткая строка: что именно нарушено/применимо и почему.
- ОПИРАЙСЯ ТОЛЬКО на предоставленные фрагменты норм. Каждый пункт — следствие конкретного фрагмента.
- Учитывай ВСЕ релевантные предоставленные нормы (в них могут быть условия, исключения, санкции — не упусти важное).
- Ссылайся на номер статьи ТОЧНО как в фрагменте (например «ст. 17»). НИКОГДА не выдумывай номера статей и нормы, которых нет во фрагментах.
- Только внутриигровое право RP-сервера, не реальное законодательство.
- Если в предоставленных фрагментах нет нужной нормы — ПЕРВОЙ строкой дай нейтральную безопасную реплику (попроси основания/уточнение), второй честно укажи, что прямой нормы в своде не нашёл, не домысливай.
- Пиши по-русски, без преамбул, заголовков и воды. Диалог не пересказывай и не нумеруй.

ОДНОВРЕМЕННАЯ РЕЧЬ И СПОРЫ:
- Собеседники часто говорят ВНАХЛЁСТ, перебивают, повышают голос. Автоматическая диаризация при этом СЛИВАЕТ или ПУТАЕТ реплики (двое под одной меткой, один разбит на нескольких, реплики приписаны не тому).
- НЕ пытайся пословно разделить, кто что сказал. Реконструируй СУТЬ КОНФЛИКТА по общему смыслу и дай совет ПО СУТИ: что игроку сказать и сделать прямо сейчас, чтобы разрешить спор в свою пользу с опорой на нормы.

ВАЖНО ПРО ВХОДНОЙ ТЕКСТ: ситуация — это АВТОМАТИЧЕСКОЕ РАСПОЗНАВАНИЕ ЖИВОЙ РЕЧИ (микрофон + звук игры), оно НЕ идеально. Жди ослышек, искажённых и обрывочных слов, повторов, слов-паразитов и мусорных кусков.
- Восстанавливай СМЫСЛ по контексту: мысленно исправляй явные ослышки (близкие по звучанию слова — напр. «могу закушать» → «можно закрывать»), игнорируй филлеры и мусор, не цепляйся за дословную форму.
- НЕ цитируй искажённые/мусорные куски и не комментируй качество распознавания — отвечай сразу по сути.
- Если текст настолько обрывочный/бессмысленный, что ситуацию не понять — скажи это одной строкой и попроси повторить, ничего не выдумывая.

Если ситуация дана как ТРАНСКРИПТ СЦЕНЫ с метками говорящих («Вы:», «A:», «B:», «C:» — это диаризация живого диалога):
- Метка «Вы:» — это САМ ИГРОК (адвокат/прокурор/общий, в зависимости от режима). Остальные метки (A/B/C) — собеседники (сотрудник, задержанный, второй сотрудник и т.п.); их роли определи по смыслу реплик.
- ⚠️ РАЗМЕТКА ПО ГОВОРЯЩИМ НЕНАДЁЖНА (автоматическая диаризация по голосу). Метки могут быть перепутаны: ДВА человека слиты под одной меткой, ОДИН разбит на несколько, реплики приписаны не тому. НЕ доверяй меткам буквально — реконструируй, кто что сказал, по СМЫСЛУ и логике диалога; если метка явно противоречит содержанию реплики — игнорируй её. Даже метку «Вы» перепроверь по смыслу.
- Плюс реплики искажены распознаванием речи (см. выше) — учитывай оба искажения вместе и ориентируйся на ОБЩИЙ СМЫСЛ сцены, а не на отдельные слова/метки.
- Дай совет ИМЕННО игроку: что сказать/сделать прямо сейчас, с опорой на нормы.
- Не пересказывай диалог и не нумеруй реплики — сразу суть конфликта и конкретные действия."""


# === Обвязка запуска/диагностики ===

def _die(msg: str) -> None:
    print(f"\n[golden_advice] ОШИБКА: {msg}", file=sys.stderr)
    sys.exit(2)


def _load_modules():
    """Ленивая загрузка модулей проекта: вне контейнера падаем с читаемым сообщением."""
    try:
        import config  # noqa: F401
        from services import advice_service
        return config, advice_service
    except Exception as e:  # noqa: BLE001
        _die(
            f"не удалось импортировать модули проекта ({type(e).__name__}: {e}).\n"
            "Скрипт рассчитан на запуск из корня репозитория ВНУТРИ контейнера api/bot,\n"
            "где есть creds Vertex и доступ к БД:\n"
            "  docker compose exec api python -m scripts.golden_advice --mode record"
        )


def _init_vertex(cfg) -> None:
    """Инициализация genai-клиента по образцу api/main.py (lifespan)."""
    import os
    if not os.path.exists(cfg.VERTEX_CREDENTIALS_FILE):
        _die(
            f"файл сервис-аккаунта Vertex не найден: {cfg.VERTEX_CREDENTIALS_FILE}\n"
            "Без creds генерация и эмбеддинги невозможны — запускай внутри контейнера:\n"
            "  docker compose exec api python -m scripts.golden_advice --mode <...>"
        )
    try:
        from rate_limiter import init_client
        from services.sa_admin import project_id_for
        init_client(
            project_id=project_id_for(cfg.VERTEX_CREDENTIALS_FILE),
            credentials_file=cfg.VERTEX_CREDENTIALS_FILE,
            region=cfg.GCP_REGION,
        )
        print("[golden_advice] Vertex-клиент инициализирован")
    except Exception as e:  # noqa: BLE001
        _die(f"не удалось инициализировать Vertex-клиент: {e}")


async def _init_db():
    """Подключение db_manager (нужно режимам с живым ретривалом: record, pipeline-ab)."""
    try:
        from database.manager import db_manager
        await db_manager.connect()
        print("[golden_advice] db_manager подключён")
        return db_manager
    except Exception as e:  # noqa: BLE001
        _die(
            f"не удалось подключиться к БД ({e}).\n"
            "Живой ретривал требует Postgres с law_chunks — запускай внутри контейнера api/bot."
        )


# === Кейсы и фикстуры ===

def _load_cases(path: str, only: Optional[str]) -> Tuple[str, List[Dict[str, Any]]]:
    p = Path(path)
    if not p.exists():
        _die(f"файл кейсов не найден: {p} (ожидается data/golden_advice_cases.json)")
    data = json.loads(p.read_text(encoding="utf-8"))
    server_id = data.get("server_id") or "la_puerta"
    cases = data.get("cases") or []
    if not cases:
        _die(f"в {p} нет кейсов (ключ cases)")
    if only:
        wanted = {s.strip() for s in only.split(",") if s.strip()}
        cases = [c for c in cases if str(c.get("id")) in wanted]
        if not cases:
            _die(f"фильтр --only {only} не совпал ни с одним кейсом")
    return server_id, cases


def _load_fixtures(path: str) -> Dict[str, List[Dict[str, Any]]]:
    p = Path(path)
    if not p.exists():
        _die(
            f"файл фикстур не найден: {p}\n"
            "Сначала запиши их живым ретривалом: --mode record (внутри контейнера)."
        )
    data = json.loads(p.read_text(encoding="utf-8"))
    # Поддерживаем и {meta, chunks_by_case}, и голый {case_id: chunks}.
    return data.get("chunks_by_case") or data


# === Генерация (последовательно, пауза + максимум 1 ретрай — preview-квоты) ===

async def _generate_with_retry(adv, server_id: str, case: Dict[str, Any], pause: float) -> Dict[str, Any]:
    """Один generate_advice + максимум 1 ретрай. Возвращает {advice, chunks, error}."""
    last_err: Optional[Exception] = None
    for attempt in (1, 2):
        try:
            res = await adv.generate_advice(
                server_id,
                case["situation"],
                history=case.get("history") or None,
                mode=case.get("mode") or None,
            )
            await asyncio.sleep(pause)  # бережём QPM между генерациями
            return {"advice": res.get("advice") or "", "chunks": res.get("chunks") or [], "error": None}
        except Exception as e:  # noqa: BLE001
            last_err = e
            print(f"    ! попытка {attempt} упала: {e}")
            await asyncio.sleep(max(pause, 5.0))
    return {"advice": "", "chunks": [], "error": str(last_err)}


# === Монкипатчи advice_service ===

def _patch_fixture_retrieval(adv, holder: Dict[str, Any]) -> Callable[[], None]:
    """Подменяет в advice_service ретривал на возврат фикстуры (holder['chunks']).

    Если этап 3 уже вживлён (retrieve_multi/rewrite_queries в неймспейсе модуля) —
    патчим и их: retrieve_multi отдаёт ту же фикстуру, rewrite_queries → [] (fail-open
    контракта → легаси-путь), чтобы оба промпт-варианта видели ОДИНАКОВЫЕ чанки
    без лишнего вызова flash-lite.
    """
    saved: Dict[str, Any] = {}

    async def _fx_retrieve(server_id, query_text, top_k=None, *, mode=None):
        return copy.deepcopy(holder["chunks"])

    async def _fx_retrieve_multi(server_id, queries, top_k_per_query=None, *, mode=None):
        return copy.deepcopy(holder["chunks"])

    async def _fx_rewrite(situation, history=None):
        return []

    saved["retrieve"] = adv.retrieve
    adv.retrieve = _fx_retrieve
    if hasattr(adv, "retrieve_multi"):
        saved["retrieve_multi"] = adv.retrieve_multi
        adv.retrieve_multi = _fx_retrieve_multi
    if hasattr(adv, "rewrite_queries"):
        saved["rewrite_queries"] = adv.rewrite_queries
        adv.rewrite_queries = _fx_rewrite

    def _restore() -> None:
        for name, fn in saved.items():
            setattr(adv, name, fn)

    return _restore


def _patch_rewriter_off(adv) -> Callable[[], None]:
    """Принудительно выключает риврайтер: rewrite_queries → [] (fail-open → старый путь)."""
    orig = adv.rewrite_queries

    async def _off(situation, history=None):
        return []

    adv.rewrite_queries = _off

    def _restore() -> None:
        adv.rewrite_queries = orig

    return _restore


# === Оценка ответа ===

def _num_key(s: str):
    try:
        return tuple(int(p) for p in s.split("."))
    except ValueError:
        return (10 ** 9,)


def _evaluate(adv, advice_text: str, expected_refs: List[str], chunks: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Номера статей из текста ответа (боевой _ARTICLE_NUM_RE) против expected_refs и поданных норм."""
    mentioned = sorted({m.group(1) for m in adv._ARTICLE_NUM_RE.finditer(advice_text or "")}, key=_num_key)
    in_ctx = adv._numbers_in_refs(chunks or [])
    hits = [r for r in expected_refs if r in mentioned]
    misses = [r for r in expected_refs if r not in mentioned]
    phantoms = sorted(set(mentioned) - in_ctx, key=_num_key)  # упомянуты, но вне поданных норм
    return {"mentioned": mentioned, "hits": hits, "misses": misses, "phantoms": phantoms}


# === Markdown-отчёт ===

def _sha8(text: str) -> str:
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()[:8]


def _first_line(text: str) -> str:
    lines = (text or "").strip().splitlines()
    return (lines[0][:90] if lines else "")


def _fmt_list(items: List[str]) -> str:
    return ", ".join(items) if items else "—"


def _default_report_path(mode: str) -> str:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"data/golden_advice_report_{mode}_{ts}.md"


def _write_report(path: str, lines: List[str]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[golden_advice] отчёт записан: {p}")


def _emit_ab_report(
    path: str,
    title: str,
    server_id: str,
    variant_names: List[str],
    rows: List[Dict[str, Any]],
    header_notes: List[str],
) -> None:
    """rows: [{case, by_variant: {variant: {res, ev}}}]."""
    lines: List[str] = [f"# {title}", ""]
    lines.append(f"- дата: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"- server_id: `{server_id}`; кейсов: {len(rows)}")
    lines.extend(header_notes)
    lines.append("")

    # Сводная таблица hit-rate по вариантам.
    lines.append("## Сводка")
    lines.append("")
    lines.append("| Вариант | кейсов | hits/expected | hit-rate (средн. по кейсам) | кейсов с фантомами | ошибок |")
    lines.append("|---|---|---|---|---|---|")
    for name in variant_names:
        total_hits = total_exp = n = phantom_cases = errors = 0
        case_rates: List[float] = []
        for row in rows:
            cell = row["by_variant"].get(name)
            if not cell:
                continue
            n += 1
            exp = row["case"].get("expected_refs") or []
            ev = cell["ev"]
            total_hits += len(ev["hits"])
            total_exp += len(exp)
            case_rates.append(len(ev["hits"]) / len(exp) if exp else 1.0)
            if ev["phantoms"]:
                phantom_cases += 1
            if cell["res"].get("error"):
                errors += 1
        mean_rate = (sum(case_rates) / len(case_rates)) if case_rates else 0.0
        agg = f"{total_hits}/{total_exp}"
        lines.append(f"| {name} | {n} | {agg} | {mean_rate:.0%} | {phantom_cases} | {errors} |")
    lines.append("")
    lines.append("Hit = номер из expected_refs упомянут в ответе (по боевому `_ARTICLE_NUM_RE`). "
                 "Фантом = номер статьи в ответе, отсутствующий в поданных нормах. "
                 "Качество формулировок (кодекс+часть, применимость «Скажи») — оцениваем руками 0/1/2 по полным ответам ниже.")
    lines.append("")

    # Подробности по кейсам.
    for row in rows:
        case = row["case"]
        lines.append(f"## Кейс {case['id']} — {case.get('comment', '')}")
        lines.append("")
        lines.append(f"- режим: `{case.get('mode')}`; ожидаемые статьи: {_fmt_list(case.get('expected_refs') or [])}")
        lines.append(f"- ситуация: `{case['situation']}`")
        if case.get("history"):
            lines.append(f"- история: `{case['history']}`")
        lines.append("")
        for name in variant_names:
            cell = row["by_variant"].get(name)
            if not cell:
                continue
            res, ev = cell["res"], cell["ev"]
            lines.append(f"### {name}")
            if res.get("error"):
                lines.append(f"- ОШИБКА вызова: {res['error']}")
            lines.append(f"- упомянутые статьи: {_fmt_list(ev['mentioned'])}")
            lines.append(f"- hit: {_fmt_list(ev['hits'])} | miss: {_fmt_list(ev['misses'])}")
            lines.append(f"- фантомные (вне поданных норм): {_fmt_list(ev['phantoms'])}")
            lines.append("")
            lines.append("Ответ:")
            lines.append("")
            lines.append("```text")
            lines.append(res.get("advice") or "(пусто)")
            lines.append("```")
            lines.append("")
    _write_report(path, lines)


# === Режимы ===

async def _run_record(args, cfg, adv) -> None:
    """Живой retrieve по каждому кейсу → фикстуры {case_id: chunks} + recall-отчёт."""
    from services.retrieval_service import retrieve

    server_id, cases = _load_cases(args.cases, args.only)
    _init_vertex(cfg)  # эмбеддинг запроса идёт через Vertex
    await _init_db()

    fixtures: Dict[str, List[Dict[str, Any]]] = {}
    lines: List[str] = [
        "# Golden advice — record (живой ретривал → фикстуры)",
        "",
        f"- дата: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- server_id: `{server_id}`; top_k={cfg.OVERLAY_RETRIEVAL_TOP_K}; "
        f"потолок контекста={getattr(cfg, 'OVERLAY_CONTEXT_MAX_CHARS', 24000)}; "
        f"эмбеддинг: `{getattr(cfg, 'EMBEDDING_MODEL_NAME', '?')}`",
        "- как в боевом пути: ретривал видит ТОЛЬКО situation (история не участвует, факт A1)",
        "",
        "## Retrieval-recall (ожидаемые статьи в выдаче ретривала)",
        "",
        "| Кейс | ожидаемые | найдены в чанках | пропущены | статей в выдаче |",
        "|---|---|---|---|---|",
    ]
    total_found = total_exp = 0
    for case in cases:
        cid = str(case["id"])
        print(f"[record] кейс {cid}: retrieve ...")
        chunks = await retrieve(
            server_id,
            case["situation"],
            top_k=cfg.OVERLAY_RETRIEVAL_TOP_K,
            mode=case.get("mode") or None,
        )
        fixtures[cid] = chunks
        in_ctx = adv._numbers_in_refs(chunks)
        expected = case.get("expected_refs") or []
        found = [r for r in expected if r in in_ctx]
        missed = [r for r in expected if r not in in_ctx]
        total_found += len(found)
        total_exp += len(expected)
        lines.append(
            f"| {cid} | {_fmt_list(expected)} | {_fmt_list(found)} | {_fmt_list(missed)} | {len(chunks)} |"
        )
        print(f"[record]   статей: {len(chunks)}, recall: {len(found)}/{len(expected)}")
        await asyncio.sleep(1.0)  # бережём QPM эмбеддингов

    lines.append("")
    rate = (total_found / total_exp) if total_exp else 0.0
    lines.append(f"**Итого recall ретривала: {total_found}/{total_exp} ({rate:.0%})**")
    lines.append("")
    lines.append("## Выдача по кейсам (article_ref → similarity)")
    lines.append("")
    for case in cases:
        cid = str(case["id"])
        lines.append(f"### Кейс {cid}")
        for ch in fixtures[cid]:
            ref = ch.get("article_ref") or "(без ref)"
            lines.append(f"- {ref} — {ch.get('title')} (sim={float(ch.get('similarity') or 0):.3f}, "
                         f"{len(ch.get('content') or '')} симв.)")
        lines.append("")

    out = {
        "meta": {
            "recorded_at": datetime.now().isoformat(timespec="seconds"),
            "server_id": server_id,
            "cases_file": args.cases,
            "top_k": cfg.OVERLAY_RETRIEVAL_TOP_K,
            "context_max_chars": getattr(cfg, "OVERLAY_CONTEXT_MAX_CHARS", 24000),
            "embedding_model": getattr(cfg, "EMBEDDING_MODEL_NAME", None),
        },
        "chunks_by_case": fixtures,
    }
    p = Path(args.out)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[golden_advice] фикстуры записаны: {p}")
    _write_report(args.report or _default_report_path("record"), lines)
    print(f"[golden_advice] recall ретривала: {total_found}/{total_exp} ({rate:.0%})")


async def _run_prompt_ab(args, cfg, adv) -> None:
    """A/B промптов current vs v3 на одинаковых чанках из фикстуры (ретривал замокан, БД не нужна)."""
    server_id, cases = _load_cases(args.cases, args.only)
    fixtures = _load_fixtures(args.fixtures)
    _init_vertex(cfg)

    current_prompt = adv.ADVICE_SYSTEM_PROMPT
    notes = [
        f"- вариант `old` = OLD_PROMPT из скрипта (sha1 {_sha8(OLD_PROMPT)}, боевой промпт ДО v3, HEAD b616f2f) — baseline",
        f"- вариант `current` = модульный ADVICE_SYSTEM_PROMPT (sha1 {_sha8(current_prompt)}; "
        f"первая строка: «{_first_line(current_prompt)}»)",
        f"- вариант `v3` = V3_PROMPT из скрипта (sha1 {_sha8(V3_PROMPT)}, вербатим docs/OVERLAY_ADVICE_PROMPTS_V3.md §1)",
        f"- фикстуры: `{args.fixtures}` (чанки одинаковые для всех вариантов; риврайтер, если вживлён, выключен)",
    ]
    if current_prompt == V3_PROMPT:
        notes.append("- ⚠️ current и v3 ИДЕНТИЧНЫ (этап 1 уже в коде) — baseline-сравнение смотри по парам old vs v3")
        print("[prompt-ab] ВНИМАНИЕ: текущий промпт уже равен v3 — baseline даёт вариант old")

    variants = [("old", OLD_PROMPT), ("current", None), ("v3", V3_PROMPT)]
    holder: Dict[str, Any] = {"chunks": []}
    restore = _patch_fixture_retrieval(adv, holder)
    rows: List[Dict[str, Any]] = []
    try:
        for case in cases:
            cid = str(case["id"])
            chunks = fixtures.get(cid)
            if chunks is None:
                print(f"[prompt-ab] кейс {cid}: нет фикстуры — пропуск (перезапиши --mode record)")
                continue
            holder["chunks"] = chunks
            row: Dict[str, Any] = {"case": case, "by_variant": {}}
            for name, prompt in variants:
                print(f"[prompt-ab] кейс {cid} × {name} ...")
                if prompt is not None:
                    adv.ADVICE_SYSTEM_PROMPT = prompt
                try:
                    res = await _generate_with_retry(adv, server_id, case, args.pause)
                finally:
                    adv.ADVICE_SYSTEM_PROMPT = current_prompt
                ev = _evaluate(adv, res["advice"], case.get("expected_refs") or [], chunks)
                row["by_variant"][name] = {"res": res, "ev": ev}
                print(f"[prompt-ab]   hit {len(ev['hits'])}/{len(case.get('expected_refs') or [])}, "
                      f"фантомы: {_fmt_list(ev['phantoms'])}")
            rows.append(row)
    finally:
        restore()

    _emit_ab_report(
        args.report or _default_report_path("prompt-ab"),
        "Golden advice — prompt-A/B (одинаковые чанки, старый vs v3 промпт)",
        server_id, [v[0] for v in variants], rows, notes,
    )


async def _run_pipeline_ab(args, cfg, adv) -> None:
    """Живой конвейер: текущий путь (риврайтер off) vs новый (rewrite_queries+retrieve_multi)."""
    server_id, cases = _load_cases(args.cases, args.only)
    _init_vertex(cfg)
    await _init_db()

    has_new = hasattr(adv, "rewrite_queries")
    variants = [("live-current", "off")]
    notes = [
        "- оба варианта — ЖИВОЙ ретривал по БД; промпт — текущий модульный "
        f"(sha1 {_sha8(adv.ADVICE_SYSTEM_PROMPT)})",
    ]
    if has_new:
        variants.append(("live-rewrite", "on"))
        notes.append("- `live-current` = риврайтер принудительно выключен (rewrite_queries → []); "
                     "`live-rewrite` = штатный новый путь (rewrite_queries + retrieve_multi)")
    else:
        msg = ("в services.advice_service НЕТ rewrite_queries/retrieve_multi — новый конвейер (этап 3) "
               "ещё не вживлён; гоняю только живой ТЕКУЩИЙ путь как baseline")
        notes.append(f"- ⚠️ {msg}")
        print(f"[pipeline-ab] {msg}")

    rows: List[Dict[str, Any]] = []
    for case in cases:
        cid = str(case["id"])
        row: Dict[str, Any] = {"case": case, "by_variant": {}}
        for name, rew in variants:
            print(f"[pipeline-ab] кейс {cid} × {name} ...")
            restore = _patch_rewriter_off(adv) if (rew == "off" and has_new) else None
            try:
                res = await _generate_with_retry(adv, server_id, case, args.pause)
            finally:
                if restore:
                    restore()
            # Фантомы считаем против чанков, реально поданных модели в ЭТОМ вызове.
            ev = _evaluate(adv, res["advice"], case.get("expected_refs") or [], res["chunks"])
            row["by_variant"][name] = {"res": res, "ev": ev}
            print(f"[pipeline-ab]   hit {len(ev['hits'])}/{len(case.get('expected_refs') or [])}, "
                  f"статей в контексте: {len(res['chunks'])}, фантомы: {_fmt_list(ev['phantoms'])}")
        rows.append(row)

    _emit_ab_report(
        args.report or _default_report_path("pipeline-ab"),
        "Golden advice — pipeline-A/B (живой ретривал: текущий путь vs риврайтер)",
        server_id, [v[0] for v in variants], rows, notes,
    )


# === Точка входа ===

async def _amain(args) -> None:
    cfg, adv = _load_modules()
    try:
        if args.mode == "record":
            await _run_record(args, cfg, adv)
        elif args.mode == "prompt-ab":
            await _run_prompt_ab(args, cfg, adv)
        elif args.mode == "pipeline-ab":
            await _run_pipeline_ab(args, cfg, adv)
    finally:
        try:
            from database.manager import db_manager
            await db_manager.close()
        except Exception:  # noqa: BLE001
            pass


def main() -> None:
    # Локальный запуск на Windows: консоль может быть не-UTF8 — чиним вывод кириллицы.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
    parser = argparse.ArgumentParser(
        prog="python -m scripts.golden_advice",
        description="Golden-set харнесс режима «Совет» оверлея (запуск внутри контейнера api/bot).",
    )
    parser.add_argument("--mode", required=True, choices=["record", "prompt-ab", "pipeline-ab"],
                        help="record: живой retrieve → фикстуры; prompt-ab: A/B промптов на фикстурах; "
                             "pipeline-ab: живой текущий путь vs риврайтер")
    parser.add_argument("--cases", default="data/golden_advice_cases.json", help="JSON с эталонными кейсами")
    parser.add_argument("--out", default="data/golden_advice_fixtures.json",
                        help="(record) куда писать фикстуры чанков")
    parser.add_argument("--fixtures", default="data/golden_advice_fixtures.json",
                        help="(prompt-ab) откуда читать фикстуры чанков")
    parser.add_argument("--report", default=None,
                        help="путь markdown-отчёта (дефолт: data/golden_advice_report_<mode>_<ts>.md)")
    parser.add_argument("--pause", type=float, default=2.5,
                        help="пауза между генерациями, сек (preview-квоты Vertex)")
    parser.add_argument("--only", default=None, help="гонять только кейсы с этими id: --only 1,2,7")
    args = parser.parse_args()
    asyncio.run(_amain(args))


if __name__ == "__main__":
    main()

# END OF FILE scripts/golden_advice.py
