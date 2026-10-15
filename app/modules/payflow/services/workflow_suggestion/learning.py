"""Learn from this client's past reviews (Suggest only) — code, no AI.

"Similar" = same situation (Active / Promise to Pay / Payment Plan); also the same
days-late band is more similar.

- Examples: the client's Approved / Active workflows that pass today's checks, for
  SIMILAR segments only — up to MAX_CLIENT_EXAMPLES per AI call. Free slots keep generic
  examples (prompt.py).
- Lessons: one line per review — where (segment) + what changed + why (reviewer note) —
  from all the client's portfolios. Per AI call: similar segments first, then the rest,
  newest first within each; at most MAX_LESSONS.

Same client only: feedback is never shared between clients.
The prompt presents both as guidance, not hard rules (prompt.py).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.infrastructure.database.models import PayflowStrategyModel
from app.modules.payflow.services.strategy_service import read_strategy_versions
from app.modules.payflow.services.workflow_suggestion.prompt import compact_step
from app.modules.payflow.services.workflow_suggestion.segmenter import CONTACT_STATES
from app.modules.payflow.services.workflow_suggestion.validator import (
    validate_catalog_values,
    validate_flowchart,
    validate_limits,
    validate_step_config,
)
from app.shared.enums import PayflowStrategyOrigin, PayflowStrategyStatus

MAX_CLIENT_EXAMPLES = 2
MAX_LESSONS = 15
MAX_CHANGES_PER_LESSON = 3  # a big rewrite lists many changes — keep each lesson to one short line

# Someone signed these off, so they can be shown as examples.
EXAMPLE_STATUSES = {PayflowStrategyStatus.APPROVED.value, PayflowStrategyStatus.ACTIVE.value}


def load_client_learning(db: Session, client_id: int, limits: dict[str, Any]) -> dict[str, list]:
    """{"pool": example candidates, "lessons": all lessons} for one client (all its portfolios), newest first."""
    rows = (
        db.query(PayflowStrategyModel)
        .filter(PayflowStrategyModel.client_id == client_id)
        .order_by(PayflowStrategyModel.updated_at.desc(), PayflowStrategyModel.id.desc())
        .all()
    )
    return {
        # Newest first — pick_examples relies on this order to prefer recent workflows.
        "pool": [
            _candidate(db, row)
            for row in rows
            if row.status in EXAMPLE_STATUSES and _valid_today(row, limits)
        ],
        "lessons": _lessons(db, rows),
    }


def pick_examples(pool: list[dict], segments: list[dict], portfolio_id: int) -> list[dict]:
    """Up to MAX_CLIENT_EXAMPLES candidates for SIMILAR segments only: one best match per
    segment, then the next best. No similar workflow → nothing (generic examples are used)."""
    chosen: list[dict] = []
    for segment in [*segments, *segments]:  # second pass fills a spare slot
        if len(chosen) == MAX_CLIENT_EXAMPLES:
            break
        similar = [item for item in pool if item not in chosen and _match(item["segment"], segment["segment"])]
        if similar:
            # max() keeps the first of equal ranks, and the pool is newest first → newest wins ties.
            chosen.append(max(similar, key=lambda item: _rank(item, segment, portfolio_id)))
    return chosen


def pick_lessons(lessons: list[dict], segments: list[dict]) -> list[str]:
    """Up to MAX_LESSONS lines: lessons for similar segments first, then the rest (newest first within each)."""
    def similarity(lesson: dict) -> int:
        return max((_match(lesson["segment"], segment["segment"]) for segment in segments), default=0)

    ranked = sorted(lessons, key=similarity, reverse=True)  # stable sort: keeps newest first within a level
    return [lesson["line"] for lesson in ranked[:MAX_LESSONS]]


def learned_from(examples: list[dict], lessons: list[str]) -> list[dict[str, str]]:
    """ai_context line showing what the AI learned from (empty when nothing)."""
    parts = []
    if examples:
        parts.append("examples " + ", ".join(f"#{item['id']}" for item in examples))
    if lessons:
        parts.append(f"{len(lessons)} reviewer lessons")
    return [{"label": "Learned from", "value": "; ".join(parts)}] if parts else []


# --------------------------------------------------------------------------- helpers


def _valid_today(row: PayflowStrategyModel, limits: dict[str, Any]) -> bool:
    """Old-format workflows, or ones using e.g. a channel the client turned off, are not shown."""
    workflow = {"segment_id": f"#{row.id}", "entry_node_id": row.entry_node_id, "steps": list(row.steps or [])}
    if not workflow["steps"] or validate_flowchart(workflow):
        return False
    errors = validate_catalog_values(workflow, limits) + validate_step_config(workflow) + validate_limits(workflow, limits)
    return not errors


def _candidate(db: Session, row: PayflowStrategyModel) -> dict[str, Any]:
    history = read_strategy_versions(db, row)
    notes = [
        entry["reviewer_note"]
        for entry in history
        if isinstance(entry, dict) and entry.get("reviewer_note")
    ]
    example = {
        "for_segment": _segment_label(row.segment or {}),
        "name": row.name,
        "summary": row.summary,
        "entry_node_id": row.entry_node_id,
        "steps": [compact_step(step) for step in row.steps or []],
    }
    if notes:
        example["reviewer_notes"] = notes
    return {
        "id": row.id,
        "portfolio_id": row.portfolio_id,
        "segment": row.segment or {},
        "human_touched": row.origin != PayflowStrategyOrigin.AI_PROPOSED.value,
        "example": example,
    }


def _match(have: dict, want: dict) -> int:
    """How similar two segments are: 2 = same situation and days-late band, 1 = same situation, 0 = not similar."""
    if not have.get("situation") or have.get("situation") != want.get("situation"):
        return 0
    return 2 if have.get("delinquency") == want.get("delinquency") else 1


def _rank(item: dict, segment: dict, portfolio_id: int) -> tuple:
    """Most similar → same portfolio → edited or written by people."""
    return (_match(item["segment"], segment["segment"]), item["portfolio_id"] == portfolio_id, item["human_touched"])


def _lessons(db: Session, rows: list[PayflowStrategyModel]) -> list[dict]:
    """All lessons, newest first: workflows by last update, then each one's history (stored newest first).

    [{"segment": the workflow's segment, "line": "- [where] WHAT. WHY: …"}], exact repeats removed.
    """
    lessons: dict[str, dict] = {}  # line → segment; keeps the first (newest) of exact repeats
    for row in rows:
        segment = row.segment or {}
        label = _segment_label(segment)
        history = read_strategy_versions(db, row)
        lines = [
            _lesson(entry, label, row.steps or [])
            for entry in history
            if isinstance(entry, dict)
        ]
        # Regenerate remarks are stored on the new workflow, not in its history.
        lines += [
            f"- [{label}] REVISION REQUESTED. WHY: {item['value']}"
            for item in row.ai_context or []
            if isinstance(item, dict) and item.get("label") == "Reviewer remarks" and item.get("value")
        ]
        for line in lines:
            if line:
                lessons.setdefault(line, segment)
    return [{"segment": segment, "line": line} for line, segment in lessons.items()]


def _lesson(entry: dict, label: str, steps: list[dict]) -> str | None:
    why = (entry.get("reviewer_note") or "").strip()
    changes = [str(change) for change in entry.get("changes") or []]
    note = (entry.get("note") or "").strip()
    if changes:
        shown = "; ".join(changes[:MAX_CHANGES_PER_LESSON])
        if len(changes) > MAX_CHANGES_PER_LESSON:
            shown += f" (+{len(changes) - MAX_CHANGES_PER_LESSON} more)"
        what = f"EDITED: {shown}"
    elif why and note == why:  # reject stores the reviewer's text as its note
        what = f"REJECTED: {_flow_summary(steps)}"
    elif why:
        what = "APPROVED" if note.startswith("Approved by") else "NOTE"
    else:
        return None  # plain status moves carry no feedback
    return f"- [{label}] {what}" + (f". WHY: {why}" if why else "")


def _flow_summary(steps: list[dict]) -> str:
    """One line, e.g. "Email Firm Reminder → wait 2 Days → SMS Payment Link → Human Review"."""
    parts = []
    for step in steps:
        cfg = step.get("config") or {}
        kind = step.get("kind")
        if kind == "Communication":
            parts.append(f"{cfg.get('channel')} {cfg.get('purpose')}")
        elif kind == "Wait" and isinstance(cfg.get("amount"), (int, float)):
            parts.append(f"wait {cfg['amount']:g} {cfg.get('unit') or 'Days'}")
        elif kind in ("Payment Action", "Case Action"):
            parts.append(cfg.get("action") or kind)
        elif kind == "Human Review":
            parts.append(kind)
    return " → ".join(parts) or "the proposed workflow"


def _segment_label(segment: dict[str, Any]) -> str:
    contact = segment.get("contact_state")
    parts = [
        segment.get("situation"),
        segment.get("delinquency"),
        CONTACT_STATES.get(contact) if contact else None,
        segment.get("balance_band"),
    ]
    return " · ".join(part for part in parts if part) or "All accounts"
