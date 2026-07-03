"""C1/C5: the shared event humanizer produces one legible sentence per kind,
prefers the typed reason, and covers the kinds that used to render blank."""

from __future__ import annotations

from exocortex.contracts import Event, EventKind
from exocortex.observability.humanize import humanize_event


def test_prefers_typed_reason() -> None:
    ev = Event(
        kind=EventKind.HANDOFF_INITIATED,
        reason="codex → hermes (fallback)",
        payload={"from_agent": "x", "to_agent": "y"},
    )
    assert humanize_event(ev) == "codex → hermes (fallback)"


def test_handoff_without_reason_reads_from_payload() -> None:
    ev = Event(
        kind=EventKind.HANDOFF_INITIATED,
        payload={"from_agent": "codex", "to_agent": "hermes", "goal_preview": "ship it"},
    )
    got = humanize_event(ev)
    assert "codex → hermes" in got
    assert "ship it" in got


def test_previously_blank_kinds_now_render() -> None:
    # These kinds had no web formatter and rendered empty before C5.
    for kind, payload, needle in [
        (EventKind.SESSION_CLOSED, {}, "session closed"),
        (EventKind.APPROVAL_RESOLVED, {"resolution": "approved"}, "approved"),
        (EventKind.PROFILE_ANSWERED, {"dimension": "tone"}, "tone"),
        (EventKind.CONVERSATION_CLOSED, {}, "conversation closed"),
    ]:
        assert needle in humanize_event(Event(kind=kind, payload=payload))


def test_unknown_payload_falls_back_to_compact_kv() -> None:
    ev = Event(kind=EventKind.TOOL_PROPOSED, payload={})
    # No reason, empty payload → the kind's own phrasing, never a crash.
    assert humanize_event(ev)
