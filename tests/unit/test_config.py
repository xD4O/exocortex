from __future__ import annotations

from pathlib import Path

import pytest

from exocortex.config import Settings


def test_defaults() -> None:
    settings = Settings()
    assert settings.data_dir == Path("./data")
    assert settings.log_level == "INFO"
    assert settings.log_format == "console"


def test_env_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("EXOCORTEX_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("EXOCORTEX_LOG_FORMAT", "json")
    monkeypatch.setenv("EXOCORTEX_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv(
        "EXOCORTEX_AUDIT_LOG_PATH", str(tmp_path / "d" / "audit.jsonl")
    )

    settings = Settings()
    assert settings.log_level == "DEBUG"
    assert settings.log_format == "json"
    assert settings.data_dir == tmp_path / "d"

    settings.ensure_dirs()
    assert settings.data_dir.exists()
    assert settings.audit_log_path.parent.exists()
