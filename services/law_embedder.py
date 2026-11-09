"""
services/law_embedder.py — ЛОКАЛЬНЫЙ эмбеддер каталога поиска по законке (ai-forever/FRIDA, sentence-transformers/CPU).

ОТДЕЛЬНО от services/embeddings.py (тот — облачный Vertex для law_chunks/RAG-советов; НЕ трогаем).
Здесь локальная CPU-модель через sentence-transformers, чтобы векторная нога поиска
(law_search_service._leg_vector) жила с ПЕРВОГО запроса без облачного cold-token (~11с > таймаут).

Почему sentence-transformers, а не ручной ONNX+onnxruntime: ST сам делает правильный пулинг (у FRIDA — CLS),
L2-нормализацию и понимает префиксы — снимается риск тихой порчи векторов от ручного пулинга. Цена — torch
в образе (+~2ГБ), но сервер простаивает. Пространство vector(1536), префиксы search_query/search_document.
См. память law-local-embeddings-design.

Импорты sentence_transformers/torch — ЛЕНИВЫЕ (внутри __init__): модуль импортируется без установленных
зависимостей (деплой инертным при LAW_CATALOG_EMBED_ENABLED=false, юнит-тесты без модели). Синглтон грузится
один раз в lifespan (api/main.py) или в скрипте пере-эмбеддинга. Инференс — через asyncio.to_thread.
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib.metadata
import logging
import weakref
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Optional, Sequence

import config

logger = logging.getLogger(__name__)


class LawEmbeddingInferenceTimeout(RuntimeError):
    """A background torch chunk exceeded its hard liveness deadline."""


# Every local artifact that can change tokenization, pooling or the numerical
# output of the SentenceTransformer bundle. The path separators are stable on
# every OS because the manifest uses these explicit POSIX-style names.
_MODEL_MANIFEST_FILES = (
    "1_Pooling/config.json",
    "config.json",
    "config_sentence_transformers.json",
    "merges.txt",
    "model.safetensors",
    "modules.json",
    "sentence_bert_config.json",
    "special_tokens_map.json",
    "tokenizer_config.json",
    "tokenizer.json",
    "vocab.json",
)


class _PriorityInferenceGate:
    """Один CPU-инференс за раз; ожидающие query идут раньше background batch."""

    def __init__(self) -> None:
        self._condition = asyncio.Condition()
        self._busy = False
        self._query_waiters = 0

    @asynccontextmanager
    async def slot(self, *, query: bool):
        registered = False
        async with self._condition:
            if query:
                self._query_waiters += 1
                registered = True
            try:
                await self._condition.wait_for(
                    lambda: not self._busy and (query or self._query_waiters == 0)
                )
            except BaseException:
                if registered:
                    self._query_waiters -= 1
                self._condition.notify_all()
                raise
            if registered:
                self._query_waiters -= 1
            self._busy = True
        try:
            yield
        finally:
            async with self._condition:
                self._busy = False
                self._condition.notify_all()


class LawEmbedder:
    """Обёртка над SentenceTransformer(FRIDA). Пулинг/нормализацию/префиксы делает сам ST по конфигу модели;
    блокирующий CPU-инференс выносится в поток корутинами embed_query/embed_documents."""

    def __init__(self, model_dir: str, query_prefix: str = "search_query: ",
                 doc_prefix: str = "search_document: ", threads: int = 2, batch_size: int = 64):
        from sentence_transformers import SentenceTransformer
        try:
            import torch
            if threads and threads > 0:
                torch.set_num_threads(int(threads))   # не выедать все 8 ядер под общий api
        except Exception:
            logger.debug("[LAW_EMBED] torch.set_num_threads недоступен", exc_info=True)

        self.query_prefix = query_prefix
        self.doc_prefix = doc_prefix
        self.batch_size = batch_size
        self.dims: Optional[int] = None
        self.normalized = True
        self.model_revision: Optional[str] = None
        self.weights_revision: Optional[str] = None
        self.runtime_versions: dict[str, Optional[str]] = {}
        self.pooling: Optional[str] = None
        self._gates: "weakref.WeakKeyDictionary[object, _PriorityInferenceGate]" = weakref.WeakKeyDictionary()
        self._model = SentenceTransformer(model_dir, device="cpu")
        try:
            self.dims = int(self._model.get_sentence_embedding_dimension())
        except Exception:
            self.dims = None
        self.pooling = self._detect_pooling()
        self.model_revision, self.weights_revision = self._artifact_revisions(model_dir)
        self.runtime_versions = self._detect_runtime_versions()
        self._validate_runtime_space()
        logger.info(
            "[LAW_EMBED] FRIDA загружена: %s (revision=%s, dims=%s, pooling=%s, threads=%s)",
            model_dir, self.model_revision, self.dims, self.pooling, threads,
        )

    def _detect_pooling(self) -> Optional[str]:
        """Читает фактический pooling из загруженного SentenceTransformer, не из env."""
        for module in self._model.modules():
            if module.__class__.__name__.lower() != "pooling":
                continue
            modes = []
            for attr, name in (
                ("pooling_mode_cls_token", "cls"),
                ("pooling_mode_mean_tokens", "mean"),
                ("pooling_mode_max_tokens", "max"),
                ("pooling_mode_mean_sqrt_len_tokens", "mean_sqrt_len"),
                ("pooling_mode_weightedmean_tokens", "weighted_mean"),
                ("pooling_mode_lasttoken", "last_token"),
            ):
                if bool(getattr(module, attr, False)):
                    modes.append(name)
            return "+".join(modes) if modes else None
        return None

    @staticmethod
    def _artifact_revisions(model_dir: str) -> tuple[Optional[str], Optional[str]]:
        """Return (manifest SHA, weights SHA), reading the large weights once."""
        root = Path(model_dir)
        missing = [relative for relative in _MODEL_MANIFEST_FILES if not (root / relative).is_file()]
        if missing:
            logger.error(
                "[LAW_EMBED] неполный manifest модели в %s: %s",
                model_dir,
                ", ".join(missing),
            )
            return None, None
        manifest_digest = hashlib.sha256()
        weights_digest = hashlib.sha256()
        for relative in sorted(_MODEL_MANIFEST_FILES):
            manifest_digest.update(relative.encode("utf-8"))
            manifest_digest.update(b"\0")
            with (root / relative).open("rb") as stream:
                for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
                    manifest_digest.update(chunk)
                    if relative == "model.safetensors":
                        weights_digest.update(chunk)
            manifest_digest.update(b"\0")
        return (
            f"sha256:{manifest_digest.hexdigest()}",
            f"sha256:{weights_digest.hexdigest()}",
        )

    @staticmethod
    def _detect_runtime_versions() -> dict[str, Optional[str]]:
        versions: dict[str, Optional[str]] = {}
        for package in ("sentence-transformers", "transformers", "tokenizers", "numpy"):
            try:
                versions[package] = importlib.metadata.version(package)
            except importlib.metadata.PackageNotFoundError:
                versions[package] = None
        try:
            import torch
            versions["torch"] = str(torch.__version__)
        except Exception:
            versions["torch"] = None
        return versions

    def _validate_runtime_space(self) -> None:
        """Не даёт процессу подписать чужие векторы паспортом active space."""
        from services.law_embedding_service import EMBEDDING_RECIPE_VERSION

        problems = []
        if self.dims != config.LAW_CATALOG_EMBED_DIMS:
            problems.append(f"dims actual={self.dims} expected={config.LAW_CATALOG_EMBED_DIMS}")
        if self.model_revision != config.LAW_CATALOG_EMBED_MODEL_REVISION:
            problems.append(
                f"revision actual={self.model_revision} expected={config.LAW_CATALOG_EMBED_MODEL_REVISION}"
            )
        if self.pooling != config.LAW_CATALOG_EMBED_POOLING:
            problems.append(f"pooling actual={self.pooling} expected={config.LAW_CATALOG_EMBED_POOLING}")
        if not config.LAW_CATALOG_EMBED_NORMALIZED:
            problems.append("runtime always normalizes vectors, but config says normalized=false")
        if config.LAW_CATALOG_EMBED_RECIPE_VERSION != EMBEDDING_RECIPE_VERSION:
            problems.append(
                "recipe actual=" + EMBEDDING_RECIPE_VERSION
                + " expected=" + config.LAW_CATALOG_EMBED_RECIPE_VERSION
            )
        for package, expected in config.LAW_CATALOG_EMBED_RUNTIME_VERSIONS.items():
            actual = self.runtime_versions.get(package)
            if actual != expected:
                problems.append(f"{package} actual={actual} expected={expected}")
        if problems:
            raise RuntimeError("FRIDA space passport mismatch: " + "; ".join(problems))

    def _encode(self, texts: Sequence[str], prefix: str) -> List[List[float]]:
        """Синхронный инференс: ST сам применяет префикс (prompt), CLS-пулинг и L2-норму."""
        items = [t or "" for t in texts]
        if not items:
            return []
        vecs = self._model.encode(
            items, prompt=prefix, batch_size=self.batch_size,
            normalize_embeddings=True, convert_to_numpy=True, show_progress_bar=False,
        )
        if self.dims is None and getattr(vecs, "shape", None) is not None and len(vecs):
            self.dims = int(vecs.shape[1])
        return vecs.tolist()

    async def embed_query(self, text: str) -> List[float]:
        gate = self._inference_gate()
        async with gate.slot(query=True):
            vecs = await asyncio.to_thread(self._encode, [text], self.query_prefix)
        return vecs[0] if vecs else []

    async def embed_documents(self, texts: Sequence[str]) -> List[List[float]]:
        if not texts:
            return []
        # Большой backfill режем на короткие кванты. Между ними gate отдаёт приоритет
        # пользовательскому query, поэтому даже полный backfill не выключает поиск.
        chunk_size = max(1, int(getattr(config, "LAW_EMBED_JOB_INFERENCE_CHUNK_SIZE", 8)))
        items = list(texts)
        result: List[List[float]] = []
        gate = self._inference_gate()
        for offset in range(0, len(items), chunk_size):
            async with gate.slot(query=False):
                chunk = items[offset:offset + chunk_size]
                encode_task = asyncio.create_task(
                    asyncio.to_thread(self._encode, chunk, self.doc_prefix)
                )
                try:
                    encoded = await asyncio.wait_for(
                        asyncio.shield(encode_task),
                        timeout=float(config.LAW_EMBED_JOB_CHUNK_TIMEOUT_SECONDS),
                    )
                except asyncio.TimeoutError as exc:
                    # asyncio cannot stop the native torch thread. The dedicated
                    # worker treats this exception as fatal and hard-exits, so no
                    # second inference is started next to a possibly wedged one.
                    logger.critical(
                        "[LAW_EMBED] document chunk завис дольше %.1fs (size=%d)",
                        config.LAW_EMBED_JOB_CHUNK_TIMEOUT_SECONDS,
                        len(chunk),
                    )
                    raise LawEmbeddingInferenceTimeout(
                        "FRIDA document chunk exceeded "
                        f"{config.LAW_EMBED_JOB_CHUNK_TIMEOUT_SECONDS:.1f}s"
                    ) from exc
                result.extend(encoded)
            await asyncio.sleep(0)
        return result

    def _inference_gate(self) -> _PriorityInferenceGate:
        loop = asyncio.get_running_loop()
        gate = self._gates.get(loop)
        if gate is None:
            gate = _PriorityInferenceGate()
            self._gates[loop] = gate
        return gate

    def embed_documents_sync(self, texts: Sequence[str]) -> List[List[float]]:
        """Для оффлайн-скрипта пере-эмбеддинга (уже в своём процессе, поток не нужен)."""
        return self._encode(list(texts), self.doc_prefix)


_EMBEDDER: Optional[LawEmbedder] = None


def init_law_embedder(warmup: bool = True) -> "LawEmbedder":
    """Грузит синглтон (idempotent). Вызывать в lifespan при LAW_CATALOG_EMBED_ENABLED и в скрипте
    пере-эмбеддинга. warmup=True прогоняет 1 пустышку — гасит разовый разогрев первого инференса."""
    global _EMBEDDER
    if _EMBEDDER is not None:
        return _EMBEDDER
    emb = LawEmbedder(
        model_dir=config.LAW_CATALOG_EMBED_MODEL_DIR,
        query_prefix=config.LAW_CATALOG_EMBED_QUERY_PREFIX,
        doc_prefix=config.LAW_CATALOG_EMBED_DOC_PREFIX,
        threads=config.LAW_CATALOG_EMBED_INTRA_THREADS,
    )
    if warmup:
        try:
            emb._encode(["прогрев"], emb.query_prefix)
        except Exception:
            logger.warning("[LAW_EMBED] warmup-прогон не удался (не критично)", exc_info=True)
    _EMBEDDER = emb
    return _EMBEDDER


def get_law_embedder() -> Optional["LawEmbedder"]:
    return _EMBEDDER
