from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from exocortex.contracts import Confidence, Event, EventKind
from exocortex.contracts.insight import Insight, InsightKind, SuggestedAction


def test_insight_requires_grounding() -> None:
    rid = uuid.uuid4()
    ok = Insight(kind=InsightKind.CONTRADICTION, title="t", detail="d",
                 refs=[rid], reflection_id=uuid.uuid4())
    assert ok.confidence == Confidence.INFERRED
    assert ok.suggested_action.type == "none"
    with pytest.raises(ValidationError):  # empty refs rejected
        Insight(kind=InsightKind.GAP, title="t", detail="d", refs=[],
                reflection_id=uuid.uuid4())


def test_suggested_action_supersede() -> None:
    a = SuggestedAction(type="supersede", stale_record_id=uuid.uuid4())
    assert a.type == "supersede" and a.stale_record_id is not None


def test_new_event_kinds_exist() -> None:
    for k in ("REFLECTION_STARTED", "REFLECTION_COMPLETED",
              "INSIGHT_PROPOSED", "INSIGHT_ACCEPTED", "INSIGHT_DISMISSED"):
        assert hasattr(EventKind, k)
    Event(kind=EventKind.INSIGHT_PROPOSED, payload={"insight_id": "x"})
