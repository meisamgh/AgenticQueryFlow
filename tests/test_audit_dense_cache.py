"""Bounded embedding cache reproductions without downloads or inference."""
from concurrent.futures import ThreadPoolExecutor

import pytest

from semantic_text2sql.hybrid_retrieval import FastEmbedEncoder


class Vectors:
    def __init__(self) -> None:
        self.calls = 0

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        return [[float(len(text)), 1.0] for text in texts]


def test_request_larger_than_cache_still_returns_all_vectors() -> None:
    encoder = FastEmbedEncoder(cache_size=1)
    model = Vectors()
    encoder._model = model
    assert encoder.encode(["a", "bb", "ccc", "a"]) == [
        [1.0, 1.0], [2.0, 1.0], [3.0, 1.0], [1.0, 1.0],
    ]
    assert len(encoder._cache) <= 1
    assert model.calls == 1


def test_caller_cannot_mutate_cached_embeddings() -> None:
    encoder = FastEmbedEncoder()
    model = Vectors()
    encoder._model = model
    encoder.encode(["a"])[0][0] = 100.0
    assert encoder.encode(["a"]) == [[1.0, 1.0]]
    assert model.calls == 1


def test_disabled_encoder_does_not_wait_for_stuck_initialization_lock() -> None:
    encoder = FastEmbedEncoder()
    pool = ThreadPoolExecutor(max_workers=1)
    encoder._lock.acquire()
    try:
        encoder.disable("Startup timed out")
        future = pool.submit(encoder.encode, ["a"])
        with pytest.raises(RuntimeError, match="Startup timed out"):
            future.result(timeout=0.3)
    finally:
        encoder._lock.release()
        pool.shutdown(wait=True)
