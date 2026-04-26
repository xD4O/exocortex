from __future__ import annotations

import hashlib
import math
import struct
from typing import Protocol


class EmbeddingProvider(Protocol):
    """Pluggable per CLAUDE-PLAN.MD §6 Phase 2. Real providers (OpenAI, Voyage,
    local model) implement `embed`; tests use `DeterministicEmbeddingProvider`.
    """

    dim: int

    def embed(self, text: str) -> list[float]: ...


class DeterministicEmbeddingProvider:
    """Hash-based pseudo-embedding for tests. Different text → different vector,
    but semantic similarity is NOT preserved. Never use for real retrieval.
    """

    dim: int = 16

    def embed(self, text: str) -> list[float]:
        digest_a = hashlib.sha256(text.encode("utf-8")).digest()
        digest_b = hashlib.sha256(digest_a).digest()
        raw = digest_a + digest_b  # 64 bytes for 16 floats
        vec = [(b / 127.5) - 1.0 for b in raw[: self.dim]]
        norm = math.sqrt(sum(x * x for x in vec)) or 1.0
        return [x / norm for x in vec]


def cosine_similarity(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        raise ValueError(f"dimension mismatch: {len(a)} vs {len(b)}")
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def pack_embedding(vec: list[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def unpack_embedding(blob: bytes) -> list[float]:
    n = len(blob) // 4
    return list(struct.unpack(f"<{n}f", blob))
