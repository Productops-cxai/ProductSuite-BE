"""Adaptive next-best-action handover (MD §5).

Backend calls ``decide_next_action(context)``. Until the AI team replaces the body,
this placeholder never changes the plan.
"""

from __future__ import annotations

from typing import Any


def decide_next_action(context: dict[str, Any]) -> dict[str, Any]:
    """Placeholder until the AI team delivers the real one: never changes the plan."""
    return {
        "action": "keep_plan",
        "reason": "No change",
        "decided_by": "rules",
        "confidence": None,
        "needs_approval": False,
        "replace_step_id": None,
        "follow_up_step_id": None,
        "at": None,
        "channel": None,
    }
