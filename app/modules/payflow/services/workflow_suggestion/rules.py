"""Turn PayFlow rules into workflow design limits — code, not AI.

Rules stay a separate layer and are never copied into workflows. Only rules that
limit a workflow's *shape* become design limits here (checked by the validator);
every other rule is enforced while workflows run.
"""

from __future__ import annotations

from typing import Any

from app.modules.payflow.services.strategy_service import CHANNELS
from app.modules.payflow.services.workflow_suggestion import config

MESSAGES_PER_WEEK_FIELD = "Messages Sent (7 days)"

# "Prevent communication when messages sent <operator> N" → allowed messages per week.
_WEEKLY_CAP_OFFSET = {"Greater Than or Equal To": -1, "Equals": -1, "Greater Than": 0}


def design_limits(client: dict[str, Any], rules: dict[str, list[dict]]) -> dict[str, Any]:
    return {
        "channels": [channel for channel in CHANNELS if channel in client["channels"]],
        "min_steps": config.MIN_STEPS,
        "max_steps": config.MAX_STEPS,
        "max_communications": config.MAX_COMMUNICATIONS,
        "max_messages_per_7_days": _max_messages_per_week(rules),
    }


def governing_rules(client: dict[str, Any], rules: dict[str, list[dict]]) -> list[str]:
    """Plain-text list of the rules a workflow is governed by (shown to reviewers)."""
    lines = []
    listed: set[str] = set()
    for kind in ("system", "client"):
        for rule in rules[kind]:
            joiner = " AND " if rule["logic"] == "ALL" else " OR "
            conditions = joiner.join(
                f"{c.get('field')} {(c.get('operator') or '').lower()} {c.get('value')}"
                for c in rule["conditions"]
            )
            lines.append(f"{rule['name']} ({kind} rule): IF {conditions} THEN {rule['action']}")
            if rule.get("name"):
                listed.add(str(rule["name"]))
    # Names still on the client but no longer matching an Active rule row.
    for name in client.get("governance_rules") or []:
        if name and str(name) not in listed:
            lines.append(f"{name} (client governance setting)")
    return lines


def _max_messages_per_week(rules: dict[str, list[dict]]) -> int | None:
    """Lowest weekly message cap set by an active "Prevent Communication" rule."""
    caps: list[int] = []
    for rule in rules["system"] + rules["client"]:
        conditions = rule["conditions"]
        # With ALL + several conditions the cap only applies sometimes — not a design limit.
        if rule["action"] != "Prevent Communication" or (rule["logic"] == "ALL" and len(conditions) > 1):
            continue
        for condition in conditions:
            offset = _WEEKLY_CAP_OFFSET.get(condition.get("operator"))
            if condition.get("field") != MESSAGES_PER_WEEK_FIELD or offset is None:
                continue
            try:
                caps.append(max(0, int(float(condition.get("value"))) + offset))
            except (TypeError, ValueError):
                continue
    return min(caps) if caps else None
