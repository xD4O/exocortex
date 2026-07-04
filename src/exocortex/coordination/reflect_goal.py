from __future__ import annotations

from exocortex.contracts import MemoryRecord


def build_reflect_goal(reflection_id: str, records: list[MemoryRecord],
                       max_insights: int) -> str:
    lines = [f"- {r.id} [{r.scope.value}/{r.source}/{r.confidence.value}] "
             f"{r.content[:300]}" for r in records]
    catalog = "\n".join(lines) if lines else "(no records in window)"
    return f"""You are exocortex's reflective analyst. Review the memory records below and \
surface INSIGHTS.

Reflection run id: {reflection_id}

For each finding, call the MCP tool `insight_propose` with reflection_id={reflection_id!r}.
Propose at most {max_insights} insights, highest-value first. Every insight MUST cite the
record UUID(s) it is grounded in via `refs` — an insight with no refs is rejected.

Insight kinds:
- contradiction: two records conflict (e.g. one says X, another not-X). suggested action_type
  "supersede" with action_payload {{"stale_record_id": "<the outdated one>"}}.
- pattern: a recurring decision/approval worth a policy rule. action_type "create_rule".
- gap: an important unanswered question. action_type "track_gap" with
  {{"question": "...", "dimension": "..."}}.
- synthesis: a durable summary of what changed / was learned. action_type "record_decision"
  with {{"content": "..."}}.

Use `memory_search` / `memory_get` if you need fuller record content. Do NOT write files or
run shell commands — the only output that matters is your `insight_propose` calls.

Records in window:
{catalog}
"""
