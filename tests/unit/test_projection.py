"""Tests for the 2D PCA projection used by the memory constellation view."""

from __future__ import annotations

from exocortex.operator.web.projection import (
    _fallback_point,
    pca_project_2d,
    project_records,
)


def test_pca_empty_input_returns_empty() -> None:
    assert pca_project_2d([]) == []


def test_pca_single_record_sits_at_origin() -> None:
    out = pca_project_2d([[0.1, 0.2, 0.3]])
    assert out == [(0.0, 0.0)]


def test_pca_projects_to_2d_and_is_deterministic() -> None:
    vecs = [
        [1.0, 0.0, 0.0, 0.0],
        [0.0, 1.0, 0.0, 0.0],
        [0.0, 0.0, 1.0, 0.0],
        [0.0, 0.0, 0.0, 1.0],
    ]
    out1 = pca_project_2d(vecs)
    out2 = pca_project_2d(vecs)
    assert len(out1) == 4
    assert all(len(p) == 2 for p in out1)
    assert out1 == out2


def test_pca_output_is_normalized() -> None:
    vecs = [[float(i), float(i % 3)] for i in range(10)]
    out = pca_project_2d(vecs)
    max_abs = max(abs(v) for xy in out for v in xy)
    assert max_abs <= 1.0 + 1e-9


def test_project_records_fallback_for_missing_embedding() -> None:
    res = project_records([("id-a", None), ("id-b", [])])
    assert len(res) == 2
    for p in res:
        assert p.has_embedding is False
        # Fallback radius < 0.5
        assert abs(p.x) < 0.5 and abs(p.y) < 0.5


def test_project_records_mixed_embedded_and_missing() -> None:
    records = [
        ("id-a", [1.0, 0.0, 0.0]),
        ("id-b", [0.0, 1.0, 0.0]),
        ("id-c", None),
        ("id-d", [0.0, 0.0, 1.0]),
    ]
    res = project_records(records)
    assert [p.id for p in res] == ["id-a", "id-b", "id-c", "id-d"]
    assert res[0].has_embedding is True
    assert res[1].has_embedding is True
    assert res[2].has_embedding is False
    assert res[3].has_embedding is True


def test_fallback_point_is_deterministic() -> None:
    assert _fallback_point("x") == _fallback_point("x")
    assert _fallback_point("x") != _fallback_point("y")
