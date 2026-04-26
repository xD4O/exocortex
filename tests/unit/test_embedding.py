from __future__ import annotations

import math

import pytest

from exocortex.memory.embedding import (
    DeterministicEmbeddingProvider,
    cosine_similarity,
    pack_embedding,
    unpack_embedding,
)


def test_deterministic_embedding_is_stable() -> None:
    p = DeterministicEmbeddingProvider()
    a = p.embed("hello world")
    b = p.embed("hello world")
    assert a == b


def test_different_text_yields_different_vectors() -> None:
    p = DeterministicEmbeddingProvider()
    assert p.embed("alpha") != p.embed("beta")


def test_vector_is_unit_length() -> None:
    p = DeterministicEmbeddingProvider()
    v = p.embed("some text")
    assert math.isclose(math.sqrt(sum(x * x for x in v)), 1.0, abs_tol=1e-6)


def test_cosine_self_similarity_is_one() -> None:
    p = DeterministicEmbeddingProvider()
    v = p.embed("same")
    assert math.isclose(cosine_similarity(v, v), 1.0, abs_tol=1e-6)


def test_cosine_rejects_dimension_mismatch() -> None:
    with pytest.raises(ValueError):
        cosine_similarity([1.0, 0.0], [1.0, 0.0, 0.0])


def test_pack_unpack_roundtrip() -> None:
    p = DeterministicEmbeddingProvider()
    v = p.embed("roundtrip")
    restored = unpack_embedding(pack_embedding(v))
    assert len(restored) == len(v)
    for a, b in zip(v, restored, strict=True):
        assert math.isclose(a, b, abs_tol=1e-6)
