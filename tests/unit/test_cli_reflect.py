from __future__ import annotations

import uuid
from pathlib import Path

import anyio
from typer.testing import CliRunner

from exocortex.contracts import Event, EventKind
from exocortex.observability.audit import AuditLog
from exocortex.operator.cli import app

runner = CliRunner()


def test_insights_list_and_dismiss(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("EXOCORTEX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("EXOCORTEX_AUDIT_LOG_PATH", str(tmp_path / "a.jsonl"))
    monkeypatch.setenv("EXOCORTEX_MEMORY_DB_PATH", str(tmp_path / "m.db"))
    iid = str(uuid.uuid4())
    anyio.run(
        AuditLog(tmp_path / "a.jsonl").record,
        Event(
            kind=EventKind.INSIGHT_PROPOSED,
            payload={
                "insight_id": iid,
                "kind": "gap",
                "title": "unanswered X",
                "detail": "d",
                "refs": [str(uuid.uuid4())],
            },
        ),
    )
    r = runner.invoke(app, ["insights"])
    assert r.exit_code == 0 and "unanswered X" in r.stdout
    r2 = runner.invoke(app, ["insights", "dismiss", iid])
    assert r2.exit_code == 0
    r3 = runner.invoke(app, ["insights"])
    assert "unanswered X" not in r3.stdout
