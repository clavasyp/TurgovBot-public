from __future__ import annotations

import asyncio
import time
import weakref

import pytest

import config
from services.law_embedder import LawEmbedder, LawEmbeddingInferenceTimeout


@pytest.mark.asyncio
async def test_document_chunk_has_hard_inference_watchdog(monkeypatch):
    """A native call may finish later, but the worker coroutine must fail boundedly."""
    embedder = object.__new__(LawEmbedder)
    embedder.doc_prefix = "search_document: "
    embedder._gates = weakref.WeakKeyDictionary()

    def slow_encode(texts, _prefix):
        time.sleep(0.06)
        return [[0.0] for _ in texts]

    embedder._encode = slow_encode
    monkeypatch.setattr(config, "LAW_EMBED_JOB_INFERENCE_CHUNK_SIZE", 4)
    monkeypatch.setattr(config, "LAW_EMBED_JOB_CHUNK_TIMEOUT_SECONDS", 0.01)

    started = time.perf_counter()
    with pytest.raises(LawEmbeddingInferenceTimeout):
        await embedder.embed_documents(["article"])
    assert time.perf_counter() - started < 0.05

    # Let the shielded test thread finish so the test process has no live work.
    await asyncio.sleep(0.07)
