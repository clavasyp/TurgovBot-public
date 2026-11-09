# Иерархическая структура данных проектов и серверов.
# Каждый проект содержит display_name, опциональные настройки и словарь серверов.
#
# Поддерживаемые поля сервера:
#   "forum_section_urls": [list]        — разделы форума с законами
#   "parse_only_author_posts": bool     — парсить только посты автора темы
#   "excluded_urls": [list]             — темы, которые нужно полностью игнорировать
#   "default_thread_post_override": dict — override для тем, НЕ перечисленных в
#       thread_post_overrides (тот же формат значения, напр. {"first_n": 1}). Автоматически
#       применяется и к новым темам раздела. Точечный thread_post_overrides имеет приоритет.
#   "thread_post_overrides": dict       — точечная настройка парсинга по конкретным темам.
#       Ключ: URL темы (или thread_id числом). Значение — один из вариантов:
#           {"first_n": 3}                                 — взять только первые 3 поста (любого автора)
#           {"post_ids": [123456, 123457]}                 — взять только посты с этими XenForo post-id
#           {"post_urls": [".../post-123456", ".../post-123457"]}  — то же, но через ссылки
#       Если override активен, parse_only_author_posts и проверка Google Doc для этой темы игнорируются.
#       Пример:
#           "thread_post_overrides": {
#               "https://forum.example.com/threads/zakon.123/": {"first_n": 4},
#               "https://forum.example.com/threads/kodeks.456/": {
#                   "post_urls": [
#                       "https://forum.example.com/threads/kodeks.456/post-9001",
#                       "https://forum.example.com/threads/kodeks.456/post-9050",
#                   ]
#               },
#           }

PROJECTS_DATA = {
    "majestic_rp": {
        "display_name": "Majestic RP",
        "description": "Официальные серверы Majestic RP",
        "forum_base_url": "https://forum.majestic-rp.ru",
        "logo_file": "logo.png",  # Логотип проекта
        "servers": {
            "new_york": {
                "display_name": "RU1 New York",
                "forum_section_urls": [
                    "https://forum.majestic-rp.ru/forums/zakonodatel-naya-baza.84/"
                ],
                "parse_only_author_posts": False
            },
            "detroit": {
                "display_name": "RU2 Detroit",
                "forum_section_urls": [
                    "https://forum.majestic-rp.ru/forums/kodeksy.353/",
                    "https://forum.majestic-rp.ru/forums/zakony.354/"
                ],
                "parse_only_author_posts": True,
                # URL темы-реестра прецедентов (если есть на сервере)
                # "precedents_registry_url": "https://forum.majestic-rp.ru/threads/precedents-registry.XXXXX/"
            },
            "chicago": {
                "display_name": "RU3 Chicago",
                "forum_section_urls": [
                    "https://forum.majestic-rp.ru/forums/zakonodatel-naya-baza.255/"
                ],
                "parse_only_author_posts": False
            },
            "san_francisco": {
                "display_name": "RU4 San Francisco",
                "forum_section_urls": [
                    "https://forum.majestic-rp.ru/forums/odobrennyye-zakonoproyekty.344/"
                ],
                "parse_only_author_posts": True
            },
            "atlanta": {
                "display_name": "RU5 Atlanta",
                "forum_section_urls": [
                    "https://forum.majestic-rp.ru/forums/odobrennyye-zakonoproyekty.562/"
                ],
                "parse_only_author_posts": True
            },
            "san_diego": {
                "display_name": "RU6 San Diego",
                "forum_section_urls": [
                    "https://forum.majestic-rp.ru/forums/normativno-pravovyye-akty.580/"
                ],
                "parse_only_author_posts": True
            },
            "los_angeles": {
                "display_name": "RU7 Los Angeles",
                "forum_section_urls": [
                    "https://forum.majestic-rp.ru/forums/zakonodatel-naya-baza.693/"
                ],
                "parse_only_author_posts": True
            },
            "miami": {
                "display_name": "RU8 Miami",
                "forum_section_urls": [
                    "https://forum.majestic-rp.ru/forums/zakonodatel-naya-baza.773/"
                ],
                "parse_only_author_posts": True
            },
            "las_vegas": {
                "display_name": "RU9 Las Vegas",
                "forum_section_urls": [
                    "https://forum.majestic-rp.ru/forums/zakonodatel-naya-baza.820/"
                ],
                "parse_only_author_posts": True
            },
            "washington": {
                "display_name": "RU10 Washington",
                "forum_section_urls": [
                    "https://forum.majestic-rp.ru/forums/odobrennyye-zakonoproyekty.895/"
                ],
                "parse_only_author_posts": True
            },
            "dallas": {
                "display_name": "RU11 Dallas",
                "forum_section_urls": [
                    "https://forum.majestic-rp.ru/forums/zakonodatel-naya-baza.954/"
                ],
                "parse_only_author_posts": True
            },
            "boston": {
                "display_name": "RU12 Boston",
                "forum_section_urls": [
                    "https://forum.majestic-rp.ru/forums/zakonodatel-naya-baza.1017/"
                ],
                "parse_only_author_posts": True
            },
            "houston": {
                "display_name": "RU13 Houston",
                "forum_section_urls": [
                    "https://forum.majestic-rp.ru/forums/zakonodatel-naya-baza.1104/"
                ],
                "parse_only_author_posts": True
            },
            "seattle": {
                "display_name": "RU14 Seattle",
                "forum_section_urls": [
                    "https://forum.majestic-rp.ru/forums/zakonodatel-naya-baza.1163/"
                ],
                "parse_only_author_posts": True
            },
            "phoenix": {
                "display_name": "RU15 Phoenix",
                "forum_section_urls": [
                    "https://forum.majestic-rp.ru/forums/zakonodatel-naya-baza.1213/"
                ],
                "parse_only_author_posts": True
            },
            "denver": {
                "display_name": "RU16 Denver",
                "forum_section_urls": [
                    "https://forum.majestic-rp.ru/forums/zakonodatel-naya-baza.1276/"
                ],
                "parse_only_author_posts": True
            },
            "portland": {
                "display_name": "RU17 Portland",
                "forum_section_urls": [
                    "https://forum.majestic-rp.ru/forums/zakonodatel-naya-baza.1338/"
                ],
                "parse_only_author_posts": True
            },
            "orlando": {
                "display_name": "RU18 Orlando",
                "forum_section_urls": [
                    "https://forum.majestic-rp.ru/forums/zakonodatel-naya-baza.1405/"
                ],
                "parse_only_author_posts": True
            },
            "memphis": {
                "display_name": "RU19 Memphis",
                "forum_section_urls": [
                    "https://forum.majestic-rp.ru/forums/zakonodatel-naya-baza.1471/"
                ],
                "parse_only_author_posts": True
            },

        }
    },
    "gta5rp": {
        "display_name": "GTA 5 RP",
        "description": "Официальные серверы GTA 5 RP",
        "forum_base_url": "https://forum.gta5rp.com",
        "logo_file": "main.png",  # Логотип проекта
        "servers": {
            "downtown": {
                "display_name": "Downtown",
                "forum_section_urls": [
                    "https://forum.gta5rp.com/forums/zakonodatelnaja-baza.803/"
                ],
                "parse_only_author_posts": True,
                "excluded_urls": [
                    "https://forum.gta5rp.com/threads/normativnye-akty-shtata-san-andreas.2879500/"
                ]
            },
            "strawberry": {
                "display_name": "Strawberry",
                "forum_section_urls": [
                    "https://forum.gta5rp.com/forums/zakonodatelnaja-baza.102/"
                ],
                "parse_only_author_posts": True,
                "excluded_urls": [
                    "https://forum.gta5rp.com/threads/informacija-ob-izmenenijax-v-zakonodatelnoi-baze.258464/"
                ]
            },
            "vinewood": {
                "display_name": "Vinewood",
                "forum_section_urls": [
                    "https://forum.gta5rp.com/forums/zakonodatelnaja-baza.170/"
                ],
                "parse_only_author_posts": True,
                "excluded_urls": []
            },
            "blackberry": {
                "display_name": "Blackberry",
                "forum_section_urls": [
                    "https://forum.gta5rp.com/forums/zakonodatelnaja-baza.262/"
                ],
                "parse_only_author_posts": True,
                "excluded_urls": [
                    "https://forum.gta5rp.com/threads/sudebnye-precedenty.1291064/"
                ]
            },
            "insquad": {
                "display_name": "Insquad",
                "forum_section_urls": [
                    "https://forum.gta5rp.com/forums/zakonodatelnaja-baza.336/"
                ],
                "parse_only_author_posts": True,
                "excluded_urls": []
            },
            "sunrise": {
                "display_name": "Sunrise",
                "forum_section_urls": [
                    "https://forum.gta5rp.com/forums/zakonodatelnaja-baza.412/"
                ],
                "parse_only_author_posts": True,
                "excluded_urls": []
            },
            "rainbow": {
                "display_name": "Rainbow",
                "forum_section_urls": [
                    "https://forum.gta5rp.com/forums/zakonodatelnaja-baza.464/"
                ],
                "parse_only_author_posts": True,
                "excluded_urls": [
                    "https://forum.gta5rp.com/threads/sa-gov-zakonodatelnaja-baza.2710147/"
                ]
            },
            "richman": {
                "display_name": "Richman",
                "forum_section_urls": [
                    "https://forum.gta5rp.com/forums/zakonodatelnaja-baza.530/"
                ],
                "parse_only_author_posts": True,
                "excluded_urls": [
                    "https://forum.gta5rp.com/threads/igrovoi-sait-laws-gov.3346313/",
                    "https://forum.gta5rp.com/threads/postanovlenija-zakonodatelnogo-sobranija.1938650/",
                    "https://forum.gta5rp.com/threads/sudebnye-precedenty-tolkovanie-npa-obzor-sudebnoi-praktiki.936690/",
                    "https://forum.gta5rp.com/threads/informacija-ob-izmenenijax-v-zakonodatelnoi-baze.934387/"
                ]
            },
            "eclipse": {
                "display_name": "Eclipse",
                "forum_section_urls": [
                    "https://forum.gta5rp.com/forums/zakonodatelnaja-baza.600/"
                ],
                "parse_only_author_posts": True,
                "excluded_urls": []
            },
            "la_mesa": {
                "display_name": "La Mesa",
                "forum_section_urls": [
                    "https://forum.gta5rp.com/forums/zakonodatelnaja-baza.651/"
                ],
                "parse_only_author_posts": True,
                "excluded_urls": []
            },
            "burton": {
                "display_name": "Burton",
                "forum_section_urls": [
                    "https://forum.gta5rp.com/forums/zakonodatelstvo.861/"
                ],
                "parse_only_author_posts": True,
                "excluded_urls": []
            },
            "rockford": {
                "display_name": "Rockford",
                "forum_section_urls": [
                    "https://forum.gta5rp.com/forums/zakonodatelstvo.910/"
                ],
                "parse_only_author_posts": True,
                "excluded_urls": [],
                "thread_post_overrides": {
                    "https://forum.gta5rp.com/threads/kodeks-ministerstva-justicii-shtata-san-andreas.2439008/": {"first_n": 4},
                    "https://forum.gta5rp.com/threads/ugolovno-administrativnyi-kodeks-shtata-san-andreas.814371/": {"first_n": 3}
                }
            },
            "alta": {
                "display_name": "Alta",
                "forum_section_urls": [
                    "https://forum.gta5rp.com/forums/zakonodatelnaja-baza.1013/"
                ],
                "parse_only_author_posts": True,
                "excluded_urls": []
            },
            "del_perro": {
                "display_name": "Del Perro",
                "forum_section_urls": [
                    "https://forum.gta5rp.com/forums/zakonodatelnaja-baza.1163/"
                ],
                "parse_only_author_posts": True,
                "excluded_urls": [
                    "https://forum.gta5rp.com/threads/informacija-ob-izmenenijax-v-zakonodatelnoi-baze.1578156/"
                ]
            },
            "davis": {
                "display_name": "Davis",
                "forum_section_urls": [
                    "https://forum.gta5rp.com/forums/zakonodatelnaja-baza.1268/"
                ],
                "parse_only_author_posts": True,
                "precedents_registry_url": "https://forum.gta5rp.com/threads/sudebnye-precedenty-i-tolkovanija.1564185/",
                "excluded_urls": [
                    "https://forum.gta5rp.com/threads/sudebnye-precedenty-i-tolkovanija.1564185/"
                ]
            },
            "harmony": {
                "display_name": "Harmony",
                "forum_section_urls": [
                    "https://forum.gta5rp.com/forums/zakonodatelnaja-baza.1369/"
                ],
                "parse_only_author_posts": True,
                "excluded_urls": [
                    "https://forum.gta5rp.com/threads/postanovlenija-kongressa.2412868/",
                    "https://forum.gta5rp.com/threads/informacija-ob-izmenenijax-v-zakonodatelnoi-baze.1755380/"
                ]
            },
            "redwood": {
                "display_name": "Redwood",
                "forum_section_urls": [
                    "https://forum.gta5rp.com/forums/zakonodatelnaja-baza.1468/"
                ],
                "parse_only_author_posts": True,
                "excluded_urls": []
            },
            "hawick": {
                "display_name": "Hawick",
                "forum_section_urls": [
                    "https://forum.gta5rp.com/forums/zakonodatelnaja-baza.1535/"
                ],
                "parse_only_author_posts": True,
                "excluded_urls": []
            },
            "grapeseed": {
                "display_name": "Grapeseed",
                "forum_section_urls": [
                    "https://forum.gta5rp.com/forums/zakonodatelnaja-baza.1644/"
                ],
                "parse_only_author_posts": True,
                "excluded_urls": [
                    "https://forum.gta5rp.com/threads/informacija-ob-izmenenijax-v-zakonodatelnoi-baze.3273338/"
                ]
            },
            "murrieta": {
                "display_name": "Murrieta",
                "forum_section_urls": [
                    "https://forum.gta5rp.com/forums/zakonodatelnaja-baza.1732/"
                ],
                "parse_only_author_posts": True,
                "excluded_urls": []
            },
            "vespucci": {
                "display_name": "Vespucci",
                "forum_section_urls": [
                    "https://forum.gta5rp.com/forums/zakonodatelnaja-baza.1821/"
                ],
                "parse_only_author_posts": True,
                "excluded_urls": [
                    "https://forum.gta5rp.com/threads/informacija-ob-izmenenijax-v-zakonodatelnoi-baze.3290721/"
                ]
            },
            "milton": {
                "display_name": "Milton",
                "forum_section_urls": [
                    "https://forum.gta5rp.com/forums/zakonodatelnaja-baza.1912/"
                ],
                "parse_only_author_posts": True,
                "excluded_urls": []
            },
            "la_puerta": {
                "display_name": "La Puerta",
                "forum_section_urls": [
                    "https://forum.gta5rp.com/forums/zakonodatelnaja-baza.1984/"
                ],
                "parse_only_author_posts": True,
                "excluded_urls": [
                    "https://forum.gta5rp.com/threads/sudebnye-precedenty-i-tolkovanija.3364599/"
                ]

            },
            "senora": {
                "display_name": "Senora",
                "forum_section_urls": [
                    "https://forum.gta5rp.com/forums/zakonodatelnaja-baza.2059/"
                ],
                "parse_only_author_posts": True,
                "excluded_urls": [
                    "https://forum.gta5rp.com/threads/normativnye-akty-shtata-san-andreas.3389197/"
                ]

            }
        }
    },
    "rmrp": {
        "display_name": "RMRP",
        "description": "RMRP — Криминальная Москва",
        "forum_base_url": "https://forum.rmrp.ru",
        "logo_file": "logo.png",
        "servers": {
            # Законодательство RMRP ЕДИНОЕ на все 5 игровых серверов (Рублёвка, Арбат,
            # Патрики, Тверской, Кутузовский) — раздел «Общее законодательство» один,
            # поэтому в конфиге один «виртуальный сервер» вместо пяти копий каталога.
            "rmrp": {
                "display_name": "Все серверы (общая база)",
                "forum_section_urls": [
                    "https://forum.rmrp.ru/forums/laws/"
                ],
                "parse_only_author_posts": True,
                # Посты 2..N в темах — changelog-и правок («Обновлено: ст. …»), сами правки
                # вносятся редактированием ПЕРВОГО поста; parse_only_author_posts их не
                # отсекает (автор постит changelog-и сам). Поэтому по умолчанию берём только
                # первый пост — в т.ч. для НОВЫХ законов, которые появятся в разделе позже.
                "default_thread_post_override": {"first_n": 1},
                "thread_post_overrides": {
                    # ФЗ «ПДД»: Глава XVI (разметка и знаки) лежит во ВТОРОМ посте темы
                    58190: {"first_n": 2},
                },
            },
        }
    },
    "russia_online": {
        "display_name": "Россия Онлайн",
        "description": "Россия Онлайн — московский RP",
        "forum_base_url": "https://forum.russia.online",
        "logo_file": "logo.png",
        # Форум XenForo за vDDoS-челленджем (тот же slowAES, что и на majestic) И ЗА ЛОГИНОМ:
        # гостю все три законодательных раздела отдают 403 «Вы должны быть авторизованы».
        # Учётка парсера — FORUM_RUSSIA_ONLINE_LOGIN/PASSWORD (см. parser_worker/worker.py, FORUM_LOGINS).
        "servers": {
            # У каждого из 3 игровых серверов СВОЯ законодательная база (разные кодексы,
            # разная нумерация, разные авторы) — в отличие от RMRP с единой базой.
            # Разделы лежат в Правительстве сервера: Организации → Гос. организации →
            # Правительство → Законодательная база. Подраздел «Отменённые законопроекты»
            # в синк НЕ попадает: discovery собирает только темы страницы, а не вложенные разделы.
            "ro_arbat": {
                "display_name": "Арбатский",
                "forum_section_urls": [
                    "https://forum.russia.online/forums/zakonodatel-naya-baza.1471/"
                ],
                "parse_only_author_posts": True,
                # Посты 2..N — приложения-скриншоты («ВИЗУАЛИЗАЦИЯ ТЕРРИТОРИЙ») и changelog-и
                # от ТОГО ЖЕ автора — parse_only_author_posts их не отсекает. Полный текст
                # закона всегда в ПЕРВОМ посте (правки вносятся его редактированием).
                "default_thread_post_override": {"first_n": 1},
            },
            "ro_tverskoy": {
                "display_name": "Тверской",
                "forum_section_urls": [
                    "https://forum.russia.online/forums/zakonodatel-naya-baza.1545/"
                ],
                "parse_only_author_posts": True,
                "default_thread_post_override": {"first_n": 1},
            },
            "ro_kutuzovsky": {
                "display_name": "Кутузовский",
                "forum_section_urls": [
                    "https://forum.russia.online/forums/zakonodatel-naya-baza.1604/"
                ],
                "parse_only_author_posts": True,
                "default_thread_post_override": {"first_n": 1},
            },
        }
    }
}



def get_flat_servers() -> dict:
    """
    Возвращает плоский словарь серверов для обратной совместимости.
    Ключи: server_id, значения: данные сервера с добавленным project_id.
    """
    flat = {}
    for project_id, project_data in PROJECTS_DATA.items():
        for server_id, server_data in project_data.get("servers", {}).items():
            flat[server_id] = {
                **server_data,
                "project_id": project_id
            }
    return flat


def get_servers_by_project(project_id: str) -> dict:
    """
    Возвращает словарь серверов для указанного проекта.
    """
    project = PROJECTS_DATA.get(project_id, {})
    return project.get("servers", {})


def get_project_for_server(server_id: str) -> str | None:
    """
    Возвращает project_id для указанного сервера.
    """
    for project_id, project_data in PROJECTS_DATA.items():
        if server_id in project_data.get("servers", {}):
            return project_id
    return None


# Обратная совместимость: LAW_SOURCES_DATA как плоский словарь
LAW_SOURCES_DATA = get_flat_servers()


# ============================================================================
#  Своды правил для режима «Обзвон» (leadership) — игровой проект → слои-namespace.
#  Отдельны от игровой законки (PROJECTS_DATA выше). Слои лежат в law_chunks под
#  server_id-namespace (project_id NULL), см. scripts/ingest_rules.py (Majestic) и
#  scripts/ingest_gta5rp_rules.py (GTA5RP). Свод для проверки лидера = UNION слоёв:
#  проектный (общие правила) + серверный (правила конкретного сервера).
#    • Majestic — единый свод на все серверы, серверный слой не нужен (servers=[]).
#    • GTA5RP  — общие правила проекта + правила выбранного сервера («показывать оба»
#      слоя без override по номеру клаузы: у слоёв разные law_document_id).
# ============================================================================

# ВНИМАНИЕ: project_layer 'majestic_rules' ДОЛЖЕН совпадать с config.LEADERSHIP_RULES_SERVER_ID
# (дефолт) — то же значение служит фолбэком для старых клиентов без project_id.
LEADERSHIP_PROJECTS = [
    {
        "project_id": "majestic_rp",
        "display_name": "Majestic RP",
        "project_layer": "majestic_rules",
        "servers": [],   # единый свод на все серверы — выбор сервера не нужен
    },
    {
        "project_id": "gta5rp",
        "display_name": "GTA5RP",
        "project_layer": "gta5rp_rules_project",
        "servers": [
            {"server_id": "davis", "display_name": "Davis", "rules_layer": "gta5rp_rules_davis"},
        ],
    },
    {
        # Россия Онлайн: общие правила проекта + «дополнительные серверные правила»
        # (Форт / военная база / Air Drop / поставки) у каждого из трёх серверов.
        # Ингест — scripts/ingest_russia_online_rules.py.
        "project_id": "russia_online",
        "display_name": "Россия Онлайн",
        "project_layer": "russia_online_rules_project",
        "servers": [
            {"server_id": "ro_arbat", "display_name": "Арбатский",
             "rules_layer": "russia_online_rules_arbat"},
            {"server_id": "ro_tverskoy", "display_name": "Тверской",
             "rules_layer": "russia_online_rules_tverskoy"},
            {"server_id": "ro_kutuzovsky", "display_name": "Кутузовский",
             "rules_layer": "russia_online_rules_kutuzovsky"},
        ],
    },
]

_LEADERSHIP_BY_PROJECT = {p["project_id"]: p for p in LEADERSHIP_PROJECTS}
# Фолбэк для неизвестного/пустого project_id (старые клиенты) — единый свод Majestic.
_LEADERSHIP_DEFAULT_LAYER = "majestic_rules"


def leadership_layers(project_id: str | None, server_id: str | None) -> list[str]:
    """Слои-namespace свода «Обзвона» для выбранного игрового проекта/сервера.

    UNION проектного слоя + (если сервер известен и покрыт) серверного. Неизвестный/пустой
    project_id → единый свод Majestic (обратная совместимость со старыми клиентами, которые
    project_id не шлют). Для Majestic серверный слой отсутствует → всегда один namespace.
    """
    proj = _LEADERSHIP_BY_PROJECT.get(project_id or "")
    if not proj:
        return [_LEADERSHIP_DEFAULT_LAYER]
    layers = [proj["project_layer"]]
    for s in proj["servers"]:
        if s["server_id"] == (server_id or ""):
            layers.append(s["rules_layer"])
            break
    return layers
