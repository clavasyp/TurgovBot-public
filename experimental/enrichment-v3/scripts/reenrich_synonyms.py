"""Перегенерация синонимов/санкций/титулов НОВЫМ промптом на СУЩЕСТВУЮЩИХ статьях (UPDATE, без DELETE).

В отличие от build_law_catalog.py --reenrich здесь статьи не пересоздаются. Но title/penalty входят
в FRIDA-input, поэтому изменившийся verified-вектор честно инвалидируется и ставится в DB-очередь.

ЗАПУСК (отдельным контейнером, чтобы деплой/рестарт api не убил; нужен VERTEX_CREDENTIALS_FILE для gemini):
  docker run -d --name reenrich-X --network turgovbot_botnet --env-file /opt/TurgovBot/.env -e DB_HOST=db \
    -e PYTHONPATH=/app -v /opt/TurgovBot/credentials:/app/credentials:ro \
    -v <this>:/app/scripts/reenrich_synonyms.py:ro ghcr.io/.../turgovbot:latest \
    python /app/scripts/reenrich_synonyms.py memphis
"""
import asyncio
import logging
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("reenrich_synonyms")


async def reenrich_server(
    server_id: str,
    *,
    manager=None,
    catalog_module=None,
):
    """Durably force and verify one complete server-wide enrichment run."""

    from database.manager import db_manager
    from services import law_catalog_service as default_catalog_module
    from services.law_document_lock import law_document_server_lock
    from services.law_embedding_service import catalog_server_lock

    manager = manager or db_manager
    catalog_module = catalog_module or default_catalog_module
    async with law_document_server_lock(server_id, manager=manager):
        async with catalog_server_lock(server_id):
            pending_claim = await catalog_module.begin_enrichment_run(
                server_id,
                invalidation="all",
            )
            return await catalog_module.run_verified_enrichment_for_server(
                server_id,
                only_stale=True,
                pending_claim=pending_claim,
            )


async def main() -> None:
    servers = sys.argv[1:] or ["memphis"]
    import config
    from rate_limiter import init_client
    from services.sa_admin import project_id_for
    from database.manager import db_manager

    init_client(project_id=project_id_for(config.VERTEX_CREDENTIALS_FILE),
                credentials_file=config.VERTEX_CREDENTIALS_FILE, region=config.GCP_REGION)
    log.info("Vertex genai-клиент инициализирован (модель обогащения: %s)", config.LAW_CATALOG_ENRICH_MODEL)
    await db_manager.connect()
    try:
        for sv in servers:
            log.info("=== reenrich синонимов НОВЫМ промптом (без пересоздания статей): %s ===", sv)
            stats = await reenrich_server(sv, manager=db_manager)
            log.info("[%s] ГОТОВО: %s", sv, stats)
    finally:
        await db_manager.close()


if __name__ == "__main__":
    asyncio.run(main())
