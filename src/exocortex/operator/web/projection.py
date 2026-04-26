"""Pure-numpy PCA for projecting memory-record embeddings into 2D for the
constellation view.

We intentionally don't pull in scikit-learn — the projection is a few lines of
numpy and keeping the dependency list short matters more than borrowed
sophistication. The projection is read-only (UI lens, never source of truth)
and is cached per-process by the caller.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class ProjectedPoint:
    id: str
    x: float
    y: float
    has_embedding: bool


def _fallback_point(record_id: str) -> tuple[float, float]:
    """Deterministic jittered origin slot for records without an embedding.

    Uses a hash so the same id always lands in the same place (stable UI) and
    different ids don't pile up on (0, 0). Radius stays < 0.5 so these points
    visibly cluster near the center, distinct from real-embedding clusters.
    """
    digest = hashlib.sha256(record_id.encode("utf-8")).digest()
    hx = int.from_bytes(digest[0:4], "little") / 0xFFFFFFFF
    hy = int.from_bytes(digest[4:8], "little") / 0xFFFFFFFF
    # Map [0,1) -> [-0.4, 0.4]
    return (hx - 0.5) * 0.8, (hy - 0.5) * 0.8


def pca_project_2d(
    embeddings: Sequence[Sequence[float]],
) -> list[tuple[float, float]]:
    """Project a list of n-dim embeddings down to 2D via PCA.

    Returns the normalized 2D coordinates in the range roughly [-1, 1].
    The output is deterministic for a given input: sign flips in the
    eigenvectors are resolved by fixing the sign of the largest-magnitude
    loading per component.
    """
    if not embeddings:
        return []
    arr = np.asarray(embeddings, dtype=np.float64)
    if arr.ndim != 2:
        raise ValueError(f"expected 2D input, got shape {arr.shape}")

    n, d = arr.shape
    if d == 0:
        return [(0.0, 0.0) for _ in range(n)]
    if n == 1:
        return [(0.0, 0.0)]

    centered = arr - arr.mean(axis=0, keepdims=True)

    # For the typical case (n is small, embedding dim is small) SVD is fast and
    # doesn't require forming d x d covariance when d < n, or n x n when n < d.
    # np.linalg.svd handles both.
    try:
        _, _, vt = np.linalg.svd(centered, full_matrices=False)
    except np.linalg.LinAlgError:
        # Degenerate data — return hashed-origin fallback on the caller side.
        return [(0.0, 0.0) for _ in range(n)]

    # Take top two right-singular vectors; if d == 1 pad a zero component.
    components = vt[:2] if vt.shape[0] >= 2 else np.vstack([vt, np.zeros_like(vt)])

    # Resolve sign ambiguity deterministically: flip each component so the
    # largest-magnitude entry is positive.
    for i in range(2):
        row = components[i]
        idx = int(np.argmax(np.abs(row)))
        if row[idx] < 0:
            components[i] = -row

    projected = centered @ components.T  # shape (n, 2)

    # Normalize to roughly [-1, 1] using max absolute value; guard against 0.
    max_abs = float(np.abs(projected).max())
    if max_abs > 0:
        projected = projected / max_abs

    return [(float(x), float(y)) for x, y in projected]


def project_records(
    records_with_embeddings: Sequence[tuple[str, Sequence[float] | None]],
) -> list[ProjectedPoint]:
    """Project a mixed list of (record_id, embedding-or-none) tuples.

    Records with embeddings are PCA-projected together; records without fall
    back to a deterministic hash-based position near the origin.
    """
    with_emb_idx: list[int] = []
    vecs: list[Sequence[float]] = []
    for i, (_rid, vec) in enumerate(records_with_embeddings):
        if vec is not None and len(vec) > 0:
            with_emb_idx.append(i)
            vecs.append(vec)

    projected_vecs = pca_project_2d(vecs) if vecs else []

    out: list[ProjectedPoint] = [
        ProjectedPoint(id="", x=0.0, y=0.0, has_embedding=False)
    ] * len(records_with_embeddings)

    # Place embedded ones
    for (idx, (x, y)) in zip(with_emb_idx, projected_vecs, strict=False):
        rid, _ = records_with_embeddings[idx]
        out[idx] = ProjectedPoint(id=rid, x=x, y=y, has_embedding=True)

    # Fallbacks
    for i, (rid, vec) in enumerate(records_with_embeddings):
        if vec is None or len(vec) == 0:
            fx, fy = _fallback_point(rid)
            out[i] = ProjectedPoint(id=rid, x=fx, y=fy, has_embedding=False)

    return out
