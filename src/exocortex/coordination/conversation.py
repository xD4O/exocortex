"""Multi-agent conversations.

A conversation is a durable room where ≥2 agents exchange messages
about a topic. The transcript is reconstructible from the audit log
alone — every turn is a `CONVERSATION_TURN` event with full payload.
The room itself is bookended by `CONVERSATION_OPENED` and (optionally)
`CONVERSATION_CLOSED` events.

This file holds the domain logic — pure functions over the audit log.
The web routes and MCP handlers above call into this. The `run_rounds`
helper turns a conversation into N rounds of dispatches, each
participant taking a turn in order, the transcript fed back as context
for each agent's reply.

Why this is the right shape for v1:
  - No new persistence layer — events are the source of truth.
  - Conversations replay from audit just like everything else.
  - Operator can inject turns as themselves to steer.
  - When the daemon eventually pushes messages live, this stays the
    same; only the delivery channel changes.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from exocortex.contracts import (
    Event,
    EventKind,
)
from exocortex.observability.audit import AuditLog


@dataclass(frozen=True)
class ConversationTurn:
    turn_id: str
    from_agent: str
    to_agent: str
    content: str
    timestamp_ms: int
    in_reply_to: str | None = None


@dataclass(frozen=True)
class Conversation:
    id: str
    topic: str
    participants: tuple[str, ...]
    status: str  # "open" | "closed"
    started_at: str
    last_activity_at: str
    turn_count: int
    last_turn_preview: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "topic": self.topic,
            "participants": list(self.participants),
            "status": self.status,
            "started_at": self.started_at,
            "last_activity_at": self.last_activity_at,
            "turn_count": self.turn_count,
            "last_turn_preview": self.last_turn_preview,
        }


class ConversationError(ValueError):
    pass


@dataclass
class ConversationService:
    audit: AuditLog

    async def open(
        self,
        *,
        topic: str,
        participants: list[str],
        opened_by: str = "operator",
    ) -> Conversation:
        if len(participants) < 2:
            raise ConversationError("conversations need ≥2 participants")
        if len(participants) != len(set(participants)):
            raise ConversationError("participants must be distinct")
        if not topic.strip():
            raise ConversationError("topic must not be empty")
        conversation_id = str(uuid4())
        ev = Event(
            kind=EventKind.CONVERSATION_OPENED,
            agent_id=opened_by,
            payload={
                "conversation_id": conversation_id,
                "topic": topic.strip(),
                "participants": list(participants),
                "opened_by": opened_by,
            },
        )
        await self.audit.record(ev)
        return Conversation(
            id=conversation_id,
            topic=topic.strip(),
            participants=tuple(participants),
            status="open",
            started_at=ev.timestamp.isoformat(),
            last_activity_at=ev.timestamp.isoformat(),
            turn_count=0,
        )

    async def add_turn(
        self,
        *,
        conversation_id: str,
        from_agent: str,
        to_agent: str,
        content: str,
        in_reply_to: str | None = None,
    ) -> ConversationTurn:
        # Verify conversation is open before recording.
        meta = await self._meta(conversation_id)
        if meta is None:
            raise ConversationError(f"conversation {conversation_id} not found")
        if meta["status"] == "deleted":
            raise ConversationError(
                f"conversation {conversation_id} was deleted"
            )
        if meta["status"] != "open":
            raise ConversationError(
                f"conversation {conversation_id} is closed; reopen by starting "
                f"a new one with the same participants if you want to continue"
            )
        if not content.strip():
            raise ConversationError("turn content must not be empty")

        turn_id = str(uuid4())
        ev = Event(
            kind=EventKind.CONVERSATION_TURN,
            agent_id=from_agent or "operator",
            payload={
                "conversation_id": conversation_id,
                "turn_id": turn_id,
                "from_agent": from_agent or "operator",
                "to_agent": to_agent,
                "content": content.strip(),
                "in_reply_to": in_reply_to,
            },
        )
        await self.audit.record(ev)
        return ConversationTurn(
            turn_id=turn_id,
            from_agent=from_agent or "operator",
            to_agent=to_agent,
            content=content.strip(),
            timestamp_ms=int(ev.timestamp.timestamp() * 1000),
            in_reply_to=in_reply_to,
        )

    async def delete(
        self, *, conversation_id: str, deleted_by: str = "operator"
    ) -> dict[str, Any]:
        """Soft-delete a conversation: emit a CONVERSATION_DELETED event
        so the conversation disappears from listings + cannot accept new
        turns, but the audit trail of what was said remains intact.

        The audit log is append-only by design (CLAUDE.md load-bearing
        rule). Hard-delete would violate that — soft-delete is honest."""
        meta = await self._meta(conversation_id)
        if meta is None:
            raise ConversationError(f"conversation {conversation_id} not found")
        if meta.get("status") == "deleted":
            return {"id": conversation_id, "status": "deleted"}
        ev = Event(
            kind=EventKind.CONVERSATION_DELETED,
            agent_id=deleted_by,
            payload={
                "conversation_id": conversation_id,
                "deleted_by": deleted_by,
                "topic_at_deletion": meta.get("topic"),
                "turn_count_at_deletion": meta.get("turn_count", 0),
            },
        )
        await self.audit.record(ev)
        return {
            "id": conversation_id,
            "status": "deleted",
            "deleted_at": ev.timestamp.isoformat(),
        }

    async def close(
        self, *, conversation_id: str, closed_by: str = "operator"
    ) -> dict[str, Any]:
        meta = await self._meta(conversation_id)
        if meta is None:
            raise ConversationError(f"conversation {conversation_id} not found")
        if meta["status"] == "closed":
            return {"id": conversation_id, "status": "closed", "closed_at": meta.get("closed_at")}
        ev = Event(
            kind=EventKind.CONVERSATION_CLOSED,
            agent_id=closed_by,
            payload={
                "conversation_id": conversation_id,
                "closed_by": closed_by,
            },
        )
        await self.audit.record(ev)
        return {
            "id": conversation_id,
            "status": "closed",
            "closed_at": ev.timestamp.isoformat(),
        }

    async def list_rooms(
        self, *, status: str = "*", limit: int = 50
    ) -> list[Conversation]:
        events = await self.audit.read_all()
        rooms = self._build_rooms(events)
        out: list[Conversation] = []
        for c in rooms.values():
            # Deleted conversations are hidden from listings unless the
            # caller explicitly asks for status="deleted" or "all-with-
            # deleted" (the latter is operator-only territory).
            if c.status == "deleted" and status != "deleted":
                continue
            if status not in ("*", c.status):
                continue
            out.append(c)
        out.sort(key=lambda c: c.last_activity_at, reverse=True)
        return out[:limit]

    async def get(self, conversation_id: str) -> dict[str, Any] | None:
        events = await self.audit.read_all()
        rooms = self._build_rooms(events)
        if conversation_id not in rooms:
            return None
        c = rooms[conversation_id]
        if c.status == "deleted":
            return None
        turns = self._turns_for(events, conversation_id)
        return {
            **c.to_dict(),
            "turns": [
                {
                    "turn_id": t.turn_id,
                    "from_agent": t.from_agent,
                    "to_agent": t.to_agent,
                    "content": t.content,
                    "timestamp_ms": t.timestamp_ms,
                    "in_reply_to": t.in_reply_to,
                }
                for t in turns
            ],
        }

    async def inbox(
        self, *, agent_id: str, limit: int = 20, since_ms: int = 0
    ) -> list[dict[str, Any]]:
        """Pending messages addressed to an agent in any open conversation,
        newest first. Agents poll this each turn (push delivery is a
        future enhancement)."""
        events = await self.audit.read_all()
        rooms = self._build_rooms(events)
        pending: list[dict[str, Any]] = []
        for ev in events:
            if ev.kind != EventKind.CONVERSATION_TURN:
                continue
            p = ev.payload or {}
            if p.get("to_agent") != agent_id:
                continue
            cid = p.get("conversation_id")
            if not isinstance(cid, str) or cid not in rooms:
                continue
            if rooms[cid].status != "open":
                continue
            ts_ms = int(ev.timestamp.timestamp() * 1000)
            if since_ms and ts_ms <= since_ms:
                continue
            pending.append(
                {
                    "conversation_id": cid,
                    "turn_id": p.get("turn_id"),
                    "from_agent": p.get("from_agent"),
                    "to_agent": p.get("to_agent"),
                    "content": p.get("content"),
                    "timestamp_ms": ts_ms,
                    "topic": rooms[cid].topic,
                }
            )
        pending.sort(key=lambda m: -int(m["timestamp_ms"]))
        return pending[:limit]

    # --- helpers -----------------------------------------------------------

    async def _meta(self, conversation_id: str) -> dict[str, Any] | None:
        events = await self.audit.read_all()
        rooms = self._build_rooms(events)
        if conversation_id not in rooms:
            return None
        c = rooms[conversation_id]
        return {
            "id": c.id,
            "status": c.status,
            "participants": list(c.participants),
            "topic": c.topic,
        }

    def _build_rooms(self, events: list[Event]) -> dict[str, Conversation]:
        rooms: dict[str, dict[str, Any]] = {}
        for ev in events:
            if ev.kind == EventKind.CONVERSATION_OPENED:
                cid = ev.payload.get("conversation_id")
                if not isinstance(cid, str):
                    continue
                rooms[cid] = {
                    "id": cid,
                    "topic": ev.payload.get("topic") or "",
                    "participants": tuple(ev.payload.get("participants") or ()),
                    "status": "open",
                    "started_at": ev.timestamp.isoformat(),
                    "last_activity_at": ev.timestamp.isoformat(),
                    "turn_count": 0,
                    "last_turn_preview": "",
                    "closed_at": None,
                }
            elif ev.kind == EventKind.CONVERSATION_TURN:
                cid = ev.payload.get("conversation_id")
                if not isinstance(cid, str) or cid not in rooms:
                    continue
                room = rooms[cid]
                room["turn_count"] = int(room.get("turn_count", 0)) + 1
                room["last_activity_at"] = ev.timestamp.isoformat()
                content = str(ev.payload.get("content") or "")
                from_agent = str(ev.payload.get("from_agent") or "")
                room["last_turn_preview"] = f"{from_agent}: {content[:80]}"
            elif ev.kind == EventKind.CONVERSATION_CLOSED:
                cid = ev.payload.get("conversation_id")
                if not isinstance(cid, str) or cid not in rooms:
                    continue
                rooms[cid]["status"] = "closed"
                rooms[cid]["closed_at"] = ev.timestamp.isoformat()
                rooms[cid]["last_activity_at"] = ev.timestamp.isoformat()
            elif ev.kind == EventKind.CONVERSATION_DELETED:
                cid = ev.payload.get("conversation_id")
                if not isinstance(cid, str) or cid not in rooms:
                    continue
                rooms[cid]["status"] = "deleted"
                rooms[cid]["deleted_at"] = ev.timestamp.isoformat()
        # Convert dicts to Conversation dataclasses.
        out: dict[str, Conversation] = {}
        for cid, room in rooms.items():
            out[cid] = Conversation(
                id=cid,
                topic=room["topic"],
                participants=tuple(room["participants"]),
                status=room["status"],
                started_at=room["started_at"],
                last_activity_at=room["last_activity_at"],
                turn_count=room["turn_count"],
                last_turn_preview=room["last_turn_preview"],
            )
        return out

    def _turns_for(
        self, events: list[Event], conversation_id: str
    ) -> list[ConversationTurn]:
        out: list[ConversationTurn] = []
        for ev in events:
            if ev.kind != EventKind.CONVERSATION_TURN:
                continue
            p = ev.payload or {}
            if p.get("conversation_id") != conversation_id:
                continue
            out.append(
                ConversationTurn(
                    turn_id=str(p.get("turn_id") or ""),
                    from_agent=str(p.get("from_agent") or ""),
                    to_agent=str(p.get("to_agent") or ""),
                    content=str(p.get("content") or ""),
                    timestamp_ms=int(ev.timestamp.timestamp() * 1000),
                    in_reply_to=p.get("in_reply_to"),
                )
            )
        out.sort(key=lambda t: t.timestamp_ms)
        return out


def _extract_agent_reply(result: dict[str, Any]) -> str:
    """Pull the agent's effective "message" out of a dispatch result so
    the orchestrator can synthesize a conversation turn even when the
    agent didn't call `conversation_turn` itself. Tries, in order:

    1. The handoff's `goal_restatement` — the agent's framing of what
       they're doing, often a one-liner that makes a decent message.
    2. The most recent decision in `decisions_so_far`.
    3. The most recent memory record the agent wrote (records[0] is
       newest by `_snapshot()` ordering — actually oldest; pick last).
    4. Empty string — caller will skip turn synthesis.
    """
    handoff = result.get("handoff") or {}
    decisions = handoff.get("decisions_so_far") or []
    if decisions:
        d = decisions[-1]
        text = (d.get("summary") or "").strip()
        if text:
            return text
    goal = (handoff.get("goal_restatement") or "").strip()
    if goal:
        return goal
    records = result.get("records") or []
    if records:
        last = records[-1]
        text = (last.get("content") or "").strip()
        if text:
            return text
    return ""


async def run_rounds(  # noqa: PLR0912, PLR0915 — orchestrator with multi-fallback paths
    *,
    service: ConversationService,
    dispatcher: Any,  # DispatchService — typed as Any to avoid circular import
    conversation_id: str,
    rounds: int = 1,
    max_wait_seconds: int = 300,
) -> list[dict[str, Any]]:
    """Fire `rounds` rounds of dispatch. Each round: every participant
    in turn order gets a dispatch with the conversation transcript as
    context. The agent's response becomes a new turn — either via the
    agent calling `conversation_turn` directly, or (fallback) by the
    orchestrator synthesizing one from the dispatch result.

    Robustness rules:
      - Agents that fail on any round are skipped on subsequent rounds
        in this run (no point retrying a dead bridge).
      - When an agent doesn't call `conversation_turn` itself, we
        synthesize one from `goal_restatement` / `decisions_so_far` /
        latest written record. Otherwise the conversation stalls
        silently — operator can't see what went wrong.
      - Per-turn timeout default raised from 120s to `max_wait_seconds`
        (default 300s) — bridges on complex prompts can take >2min.

    Returns per-turn results. Slow (10s-min per turn × N participants),
    but produces real agent dialogue without a daemon push channel.
    """
    snapshot = await service.get(conversation_id)
    if snapshot is None:
        raise ConversationError(f"conversation {conversation_id} not found")
    if snapshot["status"] != "open":
        raise ConversationError(f"conversation {conversation_id} is closed")
    participants = list(snapshot["participants"])
    if len(participants) < 2:
        raise ConversationError("conversation needs ≥2 participants to run")

    results: list[dict[str, Any]] = []
    skipped: set[str] = set()  # agents that failed in this run
    for _ in range(rounds):
        for speaker in participants:
            if speaker in skipped:
                continue
            current = await service.get(conversation_id)
            if current is None or current["status"] != "open":
                return results
            turns_before = len(current["turns"])
            others = [p for p in participants if p != speaker]
            transcript_lines = [
                f"[{t['from_agent']} → {t['to_agent']}] {t['content']}"
                for t in current["turns"]
            ]
            transcript = (
                "\n".join(transcript_lines)
                if transcript_lines
                else "(no turns yet — you go first)"
            )
            recipient = others[0] if len(others) == 1 else ", ".join(others)
            goal = (
                f"You are {speaker}. You're in a multi-agent conversation "
                f"with {recipient} about: {current['topic']}.\n\n"
                f"Transcript so far:\n{transcript}\n\n"
                f"YOUR TASK: produce ONE reply (2-4 sentences). To land "
                f"it in the transcript you MUST call this MCP tool:\n\n"
                f"  conversation_turn(\n"
                f"    conversation_id={conversation_id!r},\n"
                f"    from_agent={speaker!r},\n"
                f"    to_agent={recipient!r},\n"
                f"    content=<your message>,\n"
                f"  )\n\n"
                f"Call conversation_turn FIRST before doing anything else. "
                f"Do not write files, do not run shell commands. The only "
                f"output that matters is the conversation_turn call."
            )
            try:
                result = await dispatcher.dispatch(
                    goal=goal,
                    preferred_agent=speaker,
                    max_wait_seconds=max_wait_seconds,
                    from_agent=speaker,
                )
            except Exception as e:  # noqa: BLE001
                skipped.add(speaker)
                results.append(
                    {
                        "speaker": speaker,
                        "status": "failed",
                        "error": str(e),
                        "skipped_remaining_rounds": True,
                    }
                )
                continue

            if result.get("status") in ("failed", "timeout"):
                # Bridge ran but didn't complete cleanly. Skip this
                # speaker for the rest of the run — retrying the same
                # broken bridge wastes minutes.
                skipped.add(speaker)
                results.append(
                    {"speaker": speaker, "skipped_remaining_rounds": True, **result}
                )
                continue

            # Did the agent actually land a conversation turn?
            after = await service.get(conversation_id)
            turns_after = len(after["turns"]) if after else turns_before
            if turns_after == turns_before:
                # Agent didn't call conversation_turn — synthesize.
                synthesized = _extract_agent_reply(result)
                if synthesized:
                    try:
                        await service.add_turn(
                            conversation_id=conversation_id,
                            from_agent=speaker,
                            to_agent=recipient,
                            content=synthesized,
                        )
                        result["synthesized_turn"] = True
                    except ConversationError:
                        pass
                else:
                    result["no_reply_landed"] = True

            results.append({"speaker": speaker, **result})
            await asyncio.sleep(0.1)
    return results
