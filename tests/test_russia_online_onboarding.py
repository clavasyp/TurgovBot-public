"""Онбординг проекta «Россия Онлайн» (forum.russia.online).

Форум — XenForo за vDDoS-челленджем И за логином: гостю все три законодательных
раздела отдают 403 «Вы должны быть авторизованы». Поэтому проверяем не только конфиг
проекта, но и три вещи, на которых интеграция реально ломается:

  1. server_id нового проекта не сталкиваются с уже существующими 44 серверами
     (server_id — первичный ключ таблицы servers на все проекты сразу);
  2. вход по паролю обобщён на несколько форумов, и проверка «залогинены ли мы»
     СМОТРИТ НА ДОМЕН cookie — иначе сессия majestic выдавала бы russia.online за
     авторизованный, перелогин не срабатывал бы и разделы приезжали пустыми;
  3. хост добавлен во все списки-вайтлисты (SSRF, судебка, «Обновить дело»).
"""
import re
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
for _path in (str(_ROOT), str(_ROOT / "parser_worker")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import law_sources_data  # noqa: E402
import worker  # noqa: E402
from api import url_validator  # noqa: E402

PROJECT_ID = "russia_online"
HOST = "forum.russia.online"

EXPECTED_SERVERS = {
    "ro_arbat": "https://forum.russia.online/forums/zakonodatel-naya-baza.1471/",
    "ro_tverskoy": "https://forum.russia.online/forums/zakonodatel-naya-baza.1545/",
    "ro_kutuzovsky": "https://forum.russia.online/forums/zakonodatel-naya-baza.1604/",
}


# --- Конфиг проекта ---------------------------------------------------------

def test_project_registered_with_three_law_bases():
    project = law_sources_data.PROJECTS_DATA[PROJECT_ID]
    assert project["forum_base_url"] == "https://forum.russia.online"
    servers = project["servers"]
    assert set(servers) == set(EXPECTED_SERVERS)
    for server_id, section_url in EXPECTED_SERVERS.items():
        assert servers[server_id]["forum_section_urls"] == [section_url]


@pytest.mark.parametrize("server_id", sorted(EXPECTED_SERVERS))
def test_only_first_post_is_parsed(server_id):
    """Посты 2..N — приложения-скриншоты и changelog-и ОТ ТОГО ЖЕ автора: их не отсекает
    parse_only_author_posts, отсекает только default_thread_post_override."""
    server = law_sources_data.PROJECTS_DATA[PROJECT_ID]["servers"][server_id]
    assert server["parse_only_author_posts"] is True
    assert server["default_thread_post_override"] == {"first_n": 1}


def test_server_ids_are_unique_across_all_projects():
    """server_id — PK таблицы servers: коллизия молча перепривязала бы чужой сервер."""
    seen = []
    for project in law_sources_data.PROJECTS_DATA.values():
        seen.extend(project.get("servers", {}))
    assert len(seen) == len(set(seen))
    flat = law_sources_data.get_flat_servers()
    for server_id in EXPECTED_SERVERS:
        assert flat[server_id]["project_id"] == PROJECT_ID
        assert law_sources_data.get_project_for_server(server_id) == PROJECT_ID


# --- Вход по паролю ---------------------------------------------------------

def test_login_configured_for_russia_online():
    cfg = worker.FORUM_LOGINS[HOST]
    assert cfg["login_url"] == "https://forum.russia.online/login/"
    assert cfg["user_env"] == "FORUM_RUSSIA_ONLINE_LOGIN"
    assert cfg["password_env"] == "FORUM_RUSSIA_ONLINE_PASSWORD"
    # majestic не потерян при обобщении
    assert worker.FORUM_LOGINS["forum.majestic-rp.ru"]["user_env"] == "FORUM_MAJESTIC_LOGIN"


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://forum.russia.online/forums/zakonodatel-naya-baza.1471/", HOST),
        ("https://forum.majestic-rp.ru/threads/x.1/", "forum.majestic-rp.ru"),
        ("https://forum.gta5rp.com/threads/x.1/", None),   # публичный — вход не нужен
        ("https://forum.rmrp.ru/forums/laws/", None),      # публичный — вход не нужен
    ],
)
def test_login_host_detection(url, expected):
    assert worker._login_host(url) == expected


def test_login_wall_text_of_russia_online_is_detected():
    """Дословный ответ forum.russia.online гостю (HTTP 403, русская локаль XenForo)."""
    page = (
        "<html><head><title>Вход | Россия Онлайн Форум</title></head><body>"
        "<div class=\"blockMessage blockMessage--error\">Вы должны быть авторизованы, "
        "чтобы выполнить это действие или просмотреть эту страницу.</div></body></html>"
    )
    assert worker._is_login_wall(page) is True
    assert worker._is_login_wall("<html>Статья 1.1. Уголовное законодательство</html>") is False


class _Ctx:
    """Заглушка BrowserContext с фиксированным набором cookie."""

    def __init__(self, cookies):
        self._cookies = cookies

    async def cookies(self):
        return self._cookies


@pytest.mark.parametrize(
    "cookies, host, expected",
    [
        ([{"name": "xf_user", "domain": ".forum.russia.online"}], HOST, True),
        ([{"name": "xf_user", "domain": "forum.russia.online"}], HOST, True),
        # Ключевой случай: залогинены на majestic, но НЕ на russia.online.
        ([{"name": "xf_user", "domain": ".forum.majestic-rp.ru"}], HOST, False),
        ([{"name": "xf_session", "domain": ".forum.russia.online"}], HOST, False),
        ([], HOST, False),
    ],
)
def test_authed_check_is_scoped_to_cookie_domain(cookies, host, expected):
    import asyncio

    assert asyncio.run(worker._is_forum_authed(_Ctx(cookies), host)) is expected


def test_credentials_read_from_env(monkeypatch):
    monkeypatch.setenv("FORUM_RUSSIA_ONLINE_LOGIN", "turgovbot")
    monkeypatch.setenv("FORUM_RUSSIA_ONLINE_PASSWORD", "secret")
    assert worker._forum_credentials(HOST) == ("turgovbot", "secret")
    monkeypatch.delenv("FORUM_RUSSIA_ONLINE_PASSWORD", raising=False)
    assert worker._forum_credentials(HOST) == ("turgovbot", None)


# --- Вайтлисты хоста --------------------------------------------------------

def test_url_validator_allows_russia_online():
    ok, _ = url_validator.validate_forum_url(
        "https://forum.russia.online/threads/iskovoye-zayavleniye-no-adm-001.7556/"
    )
    assert ok is True
    bad, _ = url_validator.validate_forum_url("https://forum.example.com/threads/1/")
    assert bad is False


def test_court_parser_supports_russia_online():
    import court_case_parser

    assert court_case_parser.is_supported_forum_url(
        "https://forum.russia.online/threads/iskovoye-zayavleniye-no-adm-001.7556/"
    )
    assert court_case_parser._login_host(
        "https://forum.russia.online/threads/x.1/"
    ) == HOST
    assert court_case_parser._login_host("https://forum.rmrp.ru/threads/x.1/") is None


def test_case_refresh_regex_accepts_russia_online():
    """«Обновить дело» ищет ссылку на исходную тему в истории дела."""
    source = (_ROOT / "user_handlers" / "assistant_flow.py").read_text(encoding="utf-8")
    match = re.search(r"r'\((https\?://forum\\\.[^']+)\)'", source)
    assert match, "не найден regex ссылки на тему в assistant_flow.py"
    pattern = re.compile(match.group(1))
    assert pattern.search("тема: https://forum.russia.online/threads/delo.7556/")
    assert pattern.search("тема: https://forum.majestic-rp.ru/threads/delo.7556/")


# --- Режим «Обзвон» (своды правил) ------------------------------------------

def test_leadership_layers_union_project_and_server():
    """Обзвон отдаёт UNION: общие правила проекта + правила выбранного сервера."""
    assert law_sources_data.leadership_layers(PROJECT_ID, "ro_tverskoy") == [
        "russia_online_rules_project",
        "russia_online_rules_tverskoy",
    ]
    # Сервер не выбран → только проектный слой.
    assert law_sources_data.leadership_layers(PROJECT_ID, None) == ["russia_online_rules_project"]
    # Старые проекты не задеты.
    assert law_sources_data.leadership_layers("majestic_rp", "new_york") == ["majestic_rules"]
    assert law_sources_data.leadership_layers(None, None) == ["majestic_rules"]


def test_ingest_script_layers_match_leadership_config():
    """Namespace'ы ингест-скрипта и карты Обзвона обязаны совпадать: разъедутся —
    «Обзвон» молча вернёт пустой свод (слой есть в карте, но в law_chunks его никто не создал)."""
    source = (_ROOT / "scripts" / "ingest_russia_online_rules.py").read_text(encoding="utf-8")
    project = next(p for p in law_sources_data.LEADERSHIP_PROJECTS if p["project_id"] == PROJECT_ID)

    assert f'"{project["project_layer"]}"' in source
    for server in project["servers"]:
        assert f'"{server["rules_layer"]}"' in source, server["rules_layer"]
    # Каждому серверу проекта — свой слой правил, и все три сервера покрыты.
    assert {s["server_id"] for s in project["servers"]} == set(EXPECTED_SERVERS)
    assert len({s["rules_layer"] for s in project["servers"]}) == len(EXPECTED_SERVERS)


# --- Деперсонализация промптов ----------------------------------------------

def test_prompts_are_not_branded_as_majestic_san_andreas():
    """Проектов четыре, два из них — московский сеттинг: «штат San Andreas» в системном
    промпте был бы ложным контекстом. Правки лексические, поведение промптов не менялось."""
    import glob

    branded = re.compile(r"(?i)(majestic\s*RP|san\s*andreas|Сан-Андреас|штат[ае]?\b)")
    offenders = []
    for path in [_ROOT / "prompts.py", _ROOT / "services" / "roles.py",
                 *sorted((_ROOT / "api" / "prompts").glob("*.py"))]:
        for num, line in enumerate(path.read_text(encoding="utf-8").split("\n"), 1):
            if line.strip().startswith("#"):
                continue
            if branded.search(line):
                offenders.append(f"{path.name}:{num}")
    assert not offenders, f"брендированные строки остались: {offenders}"
