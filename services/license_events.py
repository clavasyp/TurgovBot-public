# START OF FILE services/license_events.py

"""
Серверный детект аномалий лицензии (Слой C) + алерты супер-админу по двум каналам (Слой D).

Журнал license_events (append-only, миграция 013) + детекторы по сигналам, которые УЖЕ собираются,
но раньше нигде не сравнивались:

  C2#1 дрейф HWID-компонентов  — один pubkey рапортует разное железо ⇒ спуфинг/шеринг (Риск №1 инцидента)
  C2#2 один pubkey с многих IP — шеринг ключа / невозможная география
  C2#3 велосити активаций      — выжигание ребайндов / массовая активация одного ключа
  C2#4 флаги integrity/дебага  — клиент анализируют/патчат под отладчиком
  C2#5 hwid ≠ bound_hwid в verify() — расхождение при том же pubkey (пробел инцидента §8)
  C2#6 много ключей с машины   — перепродажа/шеринг

ПРИНЦИПЫ:
  * Устойчивость: запись и детекторы — log-and-swallow (паттерн api/audit_log.py). НИКОГДА не ломают
    activate()/verify() — аудит не должен валить горячий путь.
  * Градуированная реакция (C3): soft → запись+алерт; medium → запись+усиленный алерт; hard (совокупность
    сильных сигналов) → опц. авто-бан-каскад (config.LICENSE_AUTO_BAN_ENABLED, по умолчанию ВЫКЛ — сперва
    наблюдаем точность, не «кирпичим» легит на эвристике).
  * Граница доверия: ЭСКАЛИРОВАТЬ (вплоть до бана) могут только детекторы из activate()/verify(), где
    pubkey криптографически доказан подписью challenge. Неаутентифицированная телеметрия
    (/v1/license/telemetry) только ПИШЕТ событие и шлёт алерт — forged-телеметрия с чужим pubkey не должна
    приводить к ложному бану.
  * Алерты дедуплицируются по (pubkey, набор детекторов, severity); окно зависит от severity
    (soft=LICENSE_ALERT_DEDUP_SOFT_SECONDS, medium=…_MEDIUM_…, hard=LICENSE_ALERT_DEDUP_SECONDS) —
    state-детекторы re-fire на каждом verify, короткое единое окно заливало личку.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

import httpx

import config
from database.manager import db_manager

logger = logging.getLogger(__name__)

# severity: 0 info · 1 soft · 2 medium · 3 hard
SEV_INFO, SEV_SOFT, SEV_MEDIUM, SEV_HARD = 0, 1, 2, 3
_SEV_NAME = {0: "info", 1: "soft", 2: "medium", 3: "hard"}

# Дедуп алертов: (pubkey, frozenset(detector_names), severity) → последний ts отправки. In-process,
# best-effort (несколько воркеров дедупят независимо — допустимо: хуже всего лишний дубль, не пропуск).
# severity в ключе, чтобы long-окно soft-алерта НЕ глушило эскалацию того же набора детекторов в
# medium/hard (у эскалации свой, более короткий, дедуп).
_ALERT_LAST: Dict[Tuple[str, frozenset, int], datetime] = {}
_ALERT_MAX = 4096                         # жёсткий кап _ALERT_LAST (анти-рост памяти при ротации pubkey)
_TELEM_ALERT_TS: list[datetime] = []      # окно меток глобального лимита телеметрия-алертов


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_event_id() -> str:
    return str(uuid.uuid4())


def _is_uuid(s: Any) -> bool:
    try:
        uuid.UUID(str(s))
        return True
    except Exception:
        return False


def _jb(v: Optional[Any]) -> Optional[str]:
    """JSONB-параметр: dict/list → компактный JSON-текст (+ ::jsonb каст в SQL), пусто → NULL."""
    if v is None:
        return None
    try:
        return json.dumps(v, ensure_ascii=False)
    except Exception:
        return None


def _safe_ip(ip: Optional[str]) -> Optional[str]:
    """Валидный IP-литерал для колонки INET, иначе NULL (битый ip не должен валить запись каста ::inet)."""
    if not ip:
        return None
    try:
        return str(ipaddress.ip_address(ip.strip()))
    except Exception:
        return None


def _strip_reserved(d: Optional[dict]) -> Dict[str, Any]:
    """HWID-компоненты без служебных ключей (_audit/_integrity) — только покомпонентные хеши железа."""
    return {k: v for k, v in (d or {}).items() if not str(k).startswith("_")}


def _match_ratio(stored: Optional[dict], incoming: Optional[dict]) -> Optional[float]:
    """Доля совпавших HWID-компонентов по пересечению ключей. None = сравнивать не с чем."""
    s, i = _strip_reserved(stored), _strip_reserved(incoming)
    keys = set(s) & set(i)
    if not keys:
        return None
    matched = sum(1 for k in keys if s.get(k) == i.get(k))
    return matched / len(keys)


# ---------------------------------------------------------------------------
# Запись события (append-only, дедуп по event_id)
# ---------------------------------------------------------------------------
async def record_event(
    *,
    event_type: str,
    event_id: Optional[str] = None,
    key_lookup: Optional[str] = None,
    user_id: Optional[int] = None,
    hwid: Optional[str] = None,
    hwid_pubkey: Optional[str] = None,
    ip: Optional[str] = None,
    components: Optional[dict] = None,
    app_version: Optional[str] = None,
    os: Optional[str] = None,
    integrity: Optional[dict] = None,
    severity: int = SEV_INFO,
    detail: Optional[dict] = None,
) -> Optional[str]:
    """INSERT в license_events. Дубль (второй канал дуал-сенда) игнорируется по PK event_id.
    Возвращает event_id или None при ошибке. Никогда не бросает (log-and-swallow)."""
    eid = event_id or _new_event_id()
    if not _is_uuid(eid):                 # клиент мог прислать мусор в event_id телеметрии → свой UUID
        eid = _new_event_id()
    try:
        inserted = await db_manager.fetchval(
            """
            INSERT INTO license_events
                (event_id, event_type, key_lookup, user_id, hwid, hwid_pubkey, ip,
                 components, app_version, os, integrity, severity, detail)
            VALUES ($1::uuid, $2, $3, $4, $5, $6, $7::inet,
                    $8::jsonb, $9, $10, $11::jsonb, $12, $13::jsonb)
            ON CONFLICT (event_id) DO NOTHING
            RETURNING event_id
            """,
            eid, event_type, key_lookup, user_id, hwid, hwid_pubkey, _safe_ip(ip),
            _jb(_strip_reserved(components) if components else None), app_version, os,
            _jb(integrity), int(severity), _jb(detail),
        )
        # None = конфликт по event_id (дубль второго канала дуал-сенда) → НЕ дублируем алерт/обработку.
        return str(inserted) if inserted else None
    except Exception as e:  # таблицы нет (миграция не прогнана) / БД недоступна — не валим запрос
        logger.warning("[license_events] не записал событие %s: %s", event_type, e)
        return None


async def _bump_severity(event_id: str, severity: int, detail: dict) -> None:
    try:
        await db_manager.execute(
            "UPDATE license_events SET severity=$2, detail=$3::jsonb WHERE event_id=$1::uuid",
            event_id, int(severity), _jb(detail),
        )
    except Exception as e:
        logger.warning("[license_events] не обновил severity %s: %s", event_id, e)


# ---------------------------------------------------------------------------
# Детекторы (каждый возвращает Finding | None; ошибки → None, не валят пайплайн)
# ---------------------------------------------------------------------------
class Finding:
    __slots__ = ("name", "severity", "info")

    def __init__(self, name: str, severity: int, info: dict):
        self.name, self.severity, self.info = name, severity, info


def detect_drift(incoming_components: Optional[dict], stored_components: Optional[dict]) -> Optional[Finding]:
    """C2#1: один pubkey, а компоненты «уехали» ниже порога LICENSE_FUZZY_THRESHOLD ⇒ спуфинг/шеринг."""
    ratio = _match_ratio(stored_components, incoming_components)
    if ratio is None:
        return None
    if ratio < config.LICENSE_FUZZY_THRESHOLD:
        return Finding("component_drift", SEV_MEDIUM,
                       {"match_ratio": round(ratio, 3), "threshold": config.LICENSE_FUZZY_THRESHOLD})
    return None


def detect_integrity(integrity: Optional[dict]) -> Optional[Finding]:
    """C2#4: клиент рапортует анти-дебаг/tamper-флаги ⇒ кто-то анализирует/патчит."""
    if not integrity:
        return None
    flags = [k for k in ("debugger", "remote_debugger", "peb_being_debugged", "heap_debug_flags",
                         "nt_debug_port", "nt_debug_object", "nt_debug_flags", "hw_breakpoints",
                         "parent_debugger", "tamper", "tampered") if integrity.get(k)]
    if flags:
        return Finding("client_integrity", SEV_SOFT, {"flags": flags})
    return None


def detect_hwid_mismatch(incoming_hwid: Optional[str], bound_hwid: Optional[str]) -> Optional[Finding]:
    """C2#5: тот же pubkey, но текущий hwid ≠ привязанному ⇒ подделка/нестыковка отпечатка."""
    if incoming_hwid and bound_hwid and incoming_hwid != bound_hwid:
        return Finding("hwid_vs_bound", SEV_SOFT,
                       {"incoming": incoming_hwid[-8:], "bound": bound_hwid[-8:]})
    return None


async def detect_multi_ip(hwid_pubkey: Optional[str]) -> Optional[Finding]:
    """C2#2: distinct IP на pubkey за окно ≥ порога ⇒ шеринг ключа."""
    if not hwid_pubkey:
        return None
    try:
        since = _now() - timedelta(hours=config.LICENSE_MULTI_IP_WINDOW_HOURS)
        # ТОЛЬКО подписанные события (activate/verify) — иначе НЕаутентифицированная телеметрия с
        # подделанным pubkey могла бы накрутить distinct-IP жертве и спровоцировать эскалацию (граница
        # доверия §22-25: эскалировать может только криптографически доказанный путь).
        n = await db_manager.fetchval(
            "SELECT COUNT(DISTINCT ip) FROM license_events "
            "WHERE hwid_pubkey=$1 AND event_type IN ('activate','verify') AND ip IS NOT NULL AND ts > $2",
            hwid_pubkey, since,
        )
        n = int(n or 0)
        if n >= config.LICENSE_MULTI_IP_THRESHOLD:
            sev = SEV_MEDIUM if n >= config.LICENSE_MULTI_IP_THRESHOLD * 2 else SEV_SOFT
            return Finding("multi_ip", sev,
                           {"distinct_ip": n, "window_h": config.LICENSE_MULTI_IP_WINDOW_HOURS})
    except Exception as e:
        logger.warning("[license_events] detect_multi_ip: %s", e)
    return None


async def detect_velocity(key_lookup: Optional[str]) -> Optional[Finding]:
    """C2#3: слишком много activate-событий на один ключ за окно ⇒ выжигание ребайндов/массовая активация."""
    if not key_lookup:
        return None
    try:
        since = _now() - timedelta(seconds=config.LICENSE_VELOCITY_WINDOW_SECONDS)
        n = await db_manager.fetchval(
            "SELECT COUNT(*) FROM license_events "
            "WHERE key_lookup=$1 AND event_type='activate' AND ts > $2",
            key_lookup, since,
        )
        n = int(n or 0)
        if n > config.LICENSE_ACTIVATE_VELOCITY_MAX:
            return Finding("activate_velocity", SEV_SOFT,
                           {"activations": n, "window_s": config.LICENSE_VELOCITY_WINDOW_SECONDS})
    except Exception as e:
        logger.warning("[license_events] detect_velocity: %s", e)
    return None


async def detect_keys_per_machine(hwid_pubkey: Optional[str]) -> Optional[Finding]:
    """C2#6: один pubkey засветился под несколькими разными ключами ⇒ перепродажа/шеринг.
    Окно (LICENSE_KEYS_PER_MACHINE_WINDOW_DAYS) — чтобы легитимная перепокупка ключа спустя месяцы
    не давала ВЕЧНЫЙ medium на каждом verify; перепродажа/шеринг концентрированы во времени."""
    if not hwid_pubkey:
        return None
    try:
        since = _now() - timedelta(days=config.LICENSE_KEYS_PER_MACHINE_WINDOW_DAYS)
        n = await db_manager.fetchval(
            "SELECT COUNT(DISTINCT key_lookup) FROM license_events "
            "WHERE hwid_pubkey=$1 AND key_lookup IS NOT NULL AND ts > $2",
            hwid_pubkey, since,
        )
        n = int(n or 0)
        if n > config.LICENSE_KEYS_PER_MACHINE_MAX:
            return Finding("keys_per_machine", SEV_MEDIUM, {"distinct_keys": n})
    except Exception as e:
        logger.warning("[license_events] detect_keys_per_machine: %s", e)
    return None


def _combine_severity(findings: List[Finding]) -> int:
    """Итоговый уровень: max одиночного сигнала; ДВА+ сигнала, где есть сильный (≥medium) → HARD."""
    if not findings:
        return SEV_INFO
    top = max(f.severity for f in findings)
    strong = any(f.severity >= SEV_MEDIUM for f in findings)
    if len(findings) >= 2 and strong:
        return SEV_HARD
    return top


# ---------------------------------------------------------------------------
# Главный пайплайн: записать событие, прогнать детекторы, отреагировать, алерт
# ---------------------------------------------------------------------------
async def process_signed_event(
    event_type: str,
    *,
    key_lookup: Optional[str] = None,
    user_id: Optional[int] = None,
    hwid: Optional[str] = None,
    hwid_pubkey: Optional[str] = None,
    ip: Optional[str] = None,
    components: Optional[dict] = None,
    integrity: Optional[dict] = None,
    stored_components: Optional[dict] = None,
    bound_hwid: Optional[str] = None,
    app_version: Optional[str] = None,
    os: Optional[str] = None,
) -> Dict[str, Any]:
    """Вызывается из activate()/verify() (pubkey ДОКАЗАН подписью) — детекторы могут эскалировать.
    Никогда не бросает. Возвращает {severity, findings:[names]} для лога вызывающего."""
    if not config.LICENSE_DETECT_ENABLED:
        return {}
    try:
        eid = await record_event(
            event_type=event_type, key_lookup=key_lookup, user_id=user_id, hwid=hwid,
            hwid_pubkey=hwid_pubkey, ip=ip, components=components, app_version=app_version,
            os=os, integrity=integrity,
        )

        findings: List[Finding] = []
        # синхронные (без БД)
        for f in (detect_drift(components, stored_components),
                  detect_integrity(integrity),
                  detect_hwid_mismatch(hwid, bound_hwid)):
            if f:
                findings.append(f)
        # асинхронные (БД-запросы) — параллельно; считают уже записанное выше событие
        async_findings = await asyncio.gather(
            detect_multi_ip(hwid_pubkey),
            detect_velocity(key_lookup),
            detect_keys_per_machine(hwid_pubkey),
            return_exceptions=True,
        )
        for f in async_findings:
            if isinstance(f, Finding):
                findings.append(f)

        severity = _combine_severity(findings)
        if not findings:
            return {"severity": SEV_INFO, "findings": []}

        detail = {
            "detectors": {f.name: f.info for f in findings},
            "severity_name": _SEV_NAME[severity],
        }
        if eid:
            await _bump_severity(eid, severity, detail)

        action = await _react(severity, user_id=user_id, hwid=hwid, hwid_pubkey=hwid_pubkey,
                              findings=findings)
        detail["action"] = action
        await _alert(event_type=event_type, severity=severity, user_id=user_id, hwid=hwid,
                     hwid_pubkey=hwid_pubkey, ip=ip, app_version=app_version, findings=findings,
                     action=action)
        return {"severity": severity, "findings": [f.name for f in findings]}
    except Exception as e:
        logger.warning("[license_events] process_signed_event(%s) упал: %s", event_type, e)
        return {}


async def record_anomaly_telemetry(
    *,
    event_id: Optional[str] = None,
    kind: str = "anomaly",
    detail_text: Optional[str] = None,
    hwid_pubkey: Optional[str] = None,
    hwid: Optional[str] = None,
    ip: Optional[str] = None,
    app_version: Optional[str] = None,
    os: Optional[str] = None,
    integrity: Optional[dict] = None,
) -> Dict[str, Any]:
    """Вызывается из /v1/license/telemetry (НЕаутентифицированный канал). Только ПИШЕТ событие и шлёт
    алерт при integrity/tamper-флагах — НЕ эскалирует (forged-телеметрия не должна банить)."""
    if not config.LICENSE_DETECT_ENABLED:
        return {}
    try:
        ifind = detect_integrity(integrity)
        sev = ifind.severity if ifind else SEV_SOFT  # сам факт телеметрии-аномалии = слабый сигнал
        det = {"kind": kind, "text": (detail_text or "")[:500]}
        if ifind:
            det["detectors"] = {ifind.name: ifind.info}
        eid = await record_event(
            event_type="telemetry", event_id=event_id, hwid_pubkey=hwid_pubkey, hwid=hwid, ip=ip,
            app_version=app_version, os=os, integrity=integrity, severity=sev, detail=det,
        )
        # Алерт только: (1) на СВЕЖЕЕ событие (eid не None = не дубль второго канала дуал-сенда);
        # (2) при integrity-флагах ИЛИ значимом kind; (3) под ГЛОБАЛЬНЫМ лимитом (телеметрия НЕаутент. —
        # подделанный pubkey не должен залить личку даже ротацией; дедуп по pubkey тут обходится).
        findings = [ifind] if ifind else []
        worth = bool(findings) or kind in ("tamper", "debugger", "manifest_sig", "hwid_mismatch")
        if eid and worth and _telemetry_alert_allowed():
            await _alert(event_type="telemetry", severity=sev, user_id=None, hwid=hwid,
                         hwid_pubkey=hwid_pubkey, ip=ip, app_version=app_version,
                         findings=findings or [Finding(kind, sev, det)],
                         action="(телеметрия, без эскалации)", source="telem")
        return {"severity": sev, "event_id": eid}
    except Exception as e:
        logger.warning("[license_events] record_anomaly_telemetry упал: %s", e)
        return {}


# ---------------------------------------------------------------------------
# Реакция (C3) — решение всегда серверное, hard-бан опционален и по умолчанию ВЫКЛ
# ---------------------------------------------------------------------------
async def _react(severity: int, *, user_id: Optional[int], hwid: Optional[str],
                 hwid_pubkey: Optional[str], findings: List[Finding]) -> str:
    if severity < SEV_HARD:
        # soft/medium: фиксируем + алертим; жёсткой реакции нет (не кирпичим легит)
        return "alert_only"
    # HARD: совокупность сильных сигналов
    if config.LICENSE_AUTO_BAN_ENABLED and user_id:
        try:
            from services import license as _lic  # lazy — избегаем циклического импорта
            n = await _lic.ban_hwids_for_user(user_id, banned_by=None)
            logger.warning("[license_events] HARD авто-бан user_id=%s → %s hwid(s)", user_id, n)
            return f"auto_ban_cascade(user_id={user_id}, hwids={n})"
        except Exception as e:
            logger.warning("[license_events] авто-бан упал: %s", e)
            return "auto_ban_failed"
    return "would_auto_ban(disabled)"  # LICENSE_AUTO_BAN_ENABLED=false по умолчанию


# ---------------------------------------------------------------------------
# Алерты супер-админу (Слой D) — дедуп + retry + опц. второй канал
# ---------------------------------------------------------------------------
def _dedup_window(severity: int) -> int:
    """Окно дедупа по severity: state-детекторы (multi_ip и т.п.) висят истинными сутками и
    re-fire на каждом verify — soft давим сутки, medium — часами, hard — базовое короткое окно."""
    if severity >= SEV_HARD:
        return config.LICENSE_ALERT_DEDUP_SECONDS
    if severity >= SEV_MEDIUM:
        return config.LICENSE_ALERT_DEDUP_MEDIUM_SECONDS
    return config.LICENSE_ALERT_DEDUP_SOFT_SECONDS


def _prune_alert_last() -> None:
    """Чистим устаревшие записи дедупа + жёсткий кап (иначе ротация подделанного pubkey растит память).
    Окно берём ИЗ severity ключа — иначе soft-записи (24ч) выпадали бы по короткому hard-окну и
    алерт стрелял бы заново."""
    now = _now()
    for k in [k for k, ts in _ALERT_LAST.items()
              if (now - ts).total_seconds() > _dedup_window(k[2])]:
        _ALERT_LAST.pop(k, None)
    if len(_ALERT_LAST) > _ALERT_MAX:
        for k, _ts in sorted(_ALERT_LAST.items(), key=lambda kv: kv[1])[: len(_ALERT_LAST) - _ALERT_MAX]:
            _ALERT_LAST.pop(k, None)


def _telemetry_alert_allowed() -> bool:
    """Глобальный лимит алертов из НЕаутентифицированной телеметрии (анти-флуд супер-админа)."""
    now = _now()
    cutoff = now - timedelta(seconds=60)
    while _TELEM_ALERT_TS and _TELEM_ALERT_TS[0] < cutoff:
        _TELEM_ALERT_TS.pop(0)
    if len(_TELEM_ALERT_TS) >= config.LICENSE_TELEMETRY_ALERT_MAX_PER_MIN:
        return False
    _TELEM_ALERT_TS.append(now)
    return True


def _esc(s: Any) -> str:
    t = str(s if s is not None else "")
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


async def _send_telegram(chat_id: int, html: str, attempts: int = 3) -> bool:
    """Мирроринг knowledge_base_manager.notify_super_admin, но с retry/backoff и явным chat_id."""
    token = getattr(config, "TELEGRAM_BOT_TOKEN", None)
    if not token or not chat_id:
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": html, "parse_mode": "HTML",
               "disable_web_page_preview": True}
    delay = 0.5
    for i in range(attempts):
        try:
            async with httpx.AsyncClient() as client:
                r = await client.post(url, json=payload, timeout=10.0)
            if r.status_code == 200:
                return True
            logger.warning("[license_events] alert HTTP %s (try %s)", r.status_code, i + 1)
        except Exception as e:
            logger.warning("[license_events] alert send упал (try %s): %s", i + 1, e)
        if i + 1 < attempts:
            await asyncio.sleep(delay)
            delay *= 2
    return False


async def _alert(*, event_type: str, severity: int, user_id: Optional[int], hwid: Optional[str],
                 hwid_pubkey: Optional[str], ip: Optional[str], app_version: Optional[str],
                 findings: List[Finding], action: str, source: str = "signed") -> None:
    """Дедуп по (источник+идентификатор, набор детекторов, severity) в окне; шлёт супер-админу +
    опц. в лог-канал. Антипотеря: событие УЖЕ записано в license_events до этого вызова — даже если
    Telegram лёг, тревога не потеряна (видна в журнале / дойдёт следующим алертом).
    source: 'signed' (activate/verify, pubkey доказан) | 'telem' (НЕаутентифицированная телеметрия).
    Неймспейсы дедупа у них РАЗДЕЛЬНЫЕ: иначе кованая телеметрия с pubkey жертвы занимала бы
    дедуп-ключ и на сутки глушила РЕАЛЬНЫЕ алерты подписанного пути по этой машине."""
    try:
        _prune_alert_last()
        names = frozenset(f.name for f in findings)
        if source == "telem":
            # для безыдентификаторной телеметрии подмешиваем ip — иначе все такие алерты разных
            # машин делили бы один ключ "?" и сутки глушили бы друг друга
            ident = "telem:" + (hwid_pubkey or hwid or ip or "?")
        else:
            ident = hwid_pubkey or hwid or "?"
        dkey = (ident, names, int(severity))
        now = _now()
        last = _ALERT_LAST.get(dkey)
        if last and (now - last).total_seconds() < _dedup_window(severity):
            return  # тротлинг — не заливаем при атаке
        _ALERT_LAST[dkey] = now

        emoji = {SEV_SOFT: "🟡", SEV_MEDIUM: "🟠", SEV_HARD: "🔴"}.get(severity, "⚪")
        lines = [
            f"{emoji} <b>Аномалия лицензии</b> [{_SEV_NAME.get(severity, severity)}] · {_esc(event_type)}",
        ]
        for f in findings:
            lines.append(f"• <b>{_esc(f.name)}</b>: {_esc(json.dumps(f.info, ensure_ascii=False))}")
        if user_id:
            lines.append(f"user_id: <code>{_esc(user_id)}</code>")
        if hwid:
            lines.append(f"HWID: <code>{_esc(hwid)}</code>")  # полный — для копирования и бана
        if hwid_pubkey:
            lines.append(f"pubkey: <code>{_esc(hwid_pubkey[:24])}…</code>")
        if app_version:
            lines.append(f"версия: {_esc(app_version)}  ip: {_esc(ip)}")
        lines.append(f"реакция: {_esc(action)}")
        html = "\n".join(lines)

        await _send_telegram(config.SUPER_ADMIN_ID, html)
        ch = getattr(config, "SECURITY_LOG_CHANNEL_ID", 0)
        if ch:
            await _send_telegram(ch, html)
    except Exception as e:
        logger.warning("[license_events] _alert упал: %s", e)


# END OF FILE services/license_events.py
