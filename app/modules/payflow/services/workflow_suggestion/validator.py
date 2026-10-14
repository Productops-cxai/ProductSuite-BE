"""Check AI-suggested workflows before saving — returns plain error messages.

Only checks what can be checked on a design: structure, flowchart, catalog values,
step config and limits (including design limits derived from rules).
An empty list means the workflows are valid.
"""

from __future__ import annotations

from typing import Any

from app.modules.payflow.services.strategy_service import (
    CASE_ACTIONS,
    CONDITION_ATTRIBUTES,
    CONDITION_OPERATORS,
    CONDITION_VALUES,
    MESSAGE_PURPOSES,
    MESSAGE_TEMPLATES,
    OUTCOMES,
    PAYMENT_ACTIONS,
    REFERENCE_EVENTS,
    STRATEGY_STEP_KINDS,
    TIME_DIRECTIONS,
    TIME_UNITS,
)
from app.modules.payflow.services.workflow_suggestion.config import REASONING_FIELDS
from app.modules.payflow.services.workflow_suggestion.prompt import END_STEP_KINDS

REQUIRED_REASONING = [f for f in REASONING_FIELDS if f != "changes_made"]  # changes_made: regenerate only

TIMING = ["reference_event", "amount", "unit", "direction"]
REQUIRED_CONFIG: dict[str, list[str]] = {
    "Trigger": ["reference_event"],
    "Communication": ["channel", "purpose", "template_id", *TIMING],
    "Wait": TIMING,
    "Condition": ["attribute", "operator", "value"],
    "Payment Action": ["action"],
    "Case Action": ["action"],
    "Human Review": ["note"],
    "AI Reassessment": ["note"],
    "Outcome": ["outcome"],
}
ALLOWED_CONFIG: dict[str, list[str]] = {
    "purpose": MESSAGE_PURPOSES,
    "reference_event": REFERENCE_EVENTS,
    "unit": TIME_UNITS,
    "direction": TIME_DIRECTIONS,
    "attribute": CONDITION_ATTRIBUTES,
    "operator": CONDITION_OPERATORS,
    "outcome": OUTCOMES,
}
ACTIONS_BY_KIND = {"Case Action": CASE_ACTIONS, "Payment Action": PAYMENT_ACTIONS}
TEMPLATES = {t["id"]: t for t in MESSAGE_TEMPLATES}
DAYS_PER_UNIT = {"Hours": 1 / 24, "Days": 1, "Weeks": 7}
LINKS = ("next", "yes", "no")


def validate_all(
    workflows: list[dict[str, Any]],
    segments: list[dict[str, Any]],
    limits: dict[str, Any],
) -> list[str]:
    errors = validate_structure(workflows, [s["segment_id"] for s in segments])
    for workflow in workflows:
        if not isinstance(workflow.get("steps"), list) or not workflow["steps"]:
            continue  # reported by validate_structure
        flowchart_errors = validate_flowchart(workflow)
        errors += flowchart_errors
        errors += validate_catalog_values(workflow, limits)
        errors += validate_step_config(workflow)
        errors += validate_limits(workflow, limits, check_paths=not flowchart_errors)
    return errors


def validate_structure(workflows: list[dict[str, Any]], segment_ids: list[str]) -> list[str]:
    errors: list[str] = []
    seen: set[str] = set()
    for workflow in workflows:
        segment_id = workflow.get("segment_id")
        name = _label(workflow)
        if segment_id not in segment_ids:
            errors.append(f"{name}: unknown segment_id '{segment_id}'")
        elif segment_id in seen:
            errors.append(f"{name}: more than one workflow for this segment")
        seen.add(segment_id)
        if not (workflow.get("name") or "").strip():
            errors.append(f"{name}: name is missing")
        reasoning = workflow.get("reasoning") if isinstance(workflow.get("reasoning"), dict) else {}
        missing = [f for f in REQUIRED_REASONING if not str(reasoning.get(f) or "").strip()]
        if missing:
            errors.append(f"{name}: reasoning is missing {', '.join(missing)}")
        if not isinstance(workflow.get("steps"), list) or not workflow["steps"]:
            errors.append(f"{name}: has no steps")
    errors += [f"Segment {s}: no workflow was suggested" for s in segment_ids if s not in seen]
    return errors


def validate_flowchart(workflow: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    steps = _steps_by_id(workflow)
    if len(steps) != len(workflow["steps"]):
        errors.append(f"{_label(workflow)}: step ids must be unique and not empty")

    entry = steps.get(workflow.get("entry_node_id") or "")
    if entry is None or entry.get("kind") != "Trigger":
        errors.append(f"{_label(workflow)}: entry_node_id must point to a Trigger step")

    for step_id, step in steps.items():
        where = _where(workflow, step_id)
        kind = step.get("kind")
        if kind == "Trigger" and step is not entry:
            errors.append(f"{where}: only the entry step may be a Trigger")
        for link in LINKS:
            target = step.get(link)
            if target and target not in steps:
                errors.append(f"{where}: {link} points to unknown step '{target}'")
        if kind == "Condition":
            if not (step.get("yes") and step.get("no")):
                errors.append(f"{where}: a Condition needs both yes and no")
        elif step.get("yes") or step.get("no"):
            errors.append(f"{where}: only a Condition may use yes / no (use next)")
        elif not step.get("next") and kind not in END_STEP_KINDS:
            errors.append(f"{where}: a {kind} step cannot end a path ({', '.join(END_STEP_KINDS)} only)")
        if kind == "Outcome" and step.get("next"):
            errors.append(f"{where}: an Outcome ends the path and cannot have next")

    if entry is not None:
        reachable = _reachable(steps, entry["id"])
        unreachable = [step_id for step_id in steps if step_id not in reachable]
        if unreachable:
            errors.append(f"{_label(workflow)}: steps not reachable from the entry step: {', '.join(unreachable)}")
        if _has_loop(steps, entry["id"]):
            errors.append(f"{_label(workflow)}: the flowchart contains a loop")
    return errors


def validate_catalog_values(workflow: dict[str, Any], limits: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for step in workflow["steps"]:
        where = _where(workflow, step.get("id"))
        kind = step.get("kind")
        cfg = _cfg(step)
        if kind not in STRATEGY_STEP_KINDS:
            errors.append(f"{where}: unknown step kind '{kind}'")
        for field, allowed in ALLOWED_CONFIG.items():
            if field in cfg and cfg[field] not in allowed:
                errors.append(f"{where}: {field} '{cfg[field]}' is not allowed")
        if "channel" in cfg and cfg["channel"] not in limits["channels"]:
            errors.append(f"{where}: channel '{cfg['channel']}' is not enabled for this client")
        if "amount" in cfg and not (isinstance(cfg["amount"], (int, float)) and cfg["amount"] >= 0):
            errors.append(f"{where}: amount must be a number of 0 or more")
        if "template_id" in cfg:
            template = TEMPLATES.get(cfg["template_id"])
            if template is None:
                errors.append(f"{where}: template '{cfg['template_id']}' does not exist")
            elif (template["channel"], template["purpose"]) != (cfg.get("channel"), cfg.get("purpose")):
                errors.append(
                    f"{where}: template '{template['id']}' is for {template['channel']} / "
                    f"{template['purpose']}, not {cfg.get('channel')} / {cfg.get('purpose')}"
                )
        attribute = cfg.get("attribute")
        if "value" in cfg and attribute in CONDITION_VALUES and cfg["value"] not in CONDITION_VALUES[attribute]:
            errors.append(
                f"{where}: value '{cfg['value']}' is not allowed for {attribute} "
                f"({', '.join(CONDITION_VALUES[attribute])})"
            )
        if "action" in cfg and cfg["action"] not in ACTIONS_BY_KIND.get(kind, []):
            errors.append(f"{where}: action '{cfg['action']}' is not allowed for a {kind} step")
    return errors


def validate_step_config(workflow: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for step in workflow["steps"]:
        cfg = _cfg(step)
        missing = [field for field in REQUIRED_CONFIG.get(step.get("kind"), []) if field not in cfg]
        if missing:
            errors.append(f"{_where(workflow, step.get('id'))} ({step.get('kind')}): missing {', '.join(missing)}")
    return errors


def validate_limits(workflow: dict[str, Any], limits: dict[str, Any], *, check_paths: bool = True) -> list[str]:
    errors: list[str] = []
    name = _label(workflow)
    count = len(workflow["steps"])
    if not limits["min_steps"] <= count <= limits["max_steps"]:
        errors.append(f"{name}: has {count} steps (allowed {limits['min_steps']}-{limits['max_steps']})")
    messages = [s for s in workflow["steps"] if s.get("kind") == "Communication"]
    if len(messages) > limits["max_communications"]:
        errors.append(
            f"{name}: has {len(messages)} Communication steps (max {limits['max_communications']})"
        )
    if not check_paths:
        return errors  # paths are unreliable while the flowchart itself is broken

    steps = _steps_by_id(workflow)
    for message in messages:
        follower = _next_message_without_wait(steps, message)
        if follower:
            errors.append(f"{name}: steps {message['id']} and {follower} send messages with no Wait between them")
    cap = limits.get("max_messages_per_7_days")
    if cap is not None:
        crowded = _crowded_messages(steps, workflow["entry_node_id"], cap)
        if crowded:
            errors.append(f"{name}: steps {', '.join(crowded)} send more than {cap} messages within 7 days")
    return errors


# --------------------------------------------------------------------------- helpers


def _label(workflow: dict[str, Any]) -> str:
    return f"Workflow {workflow.get('segment_id')}"


def _where(workflow: dict[str, Any], step_id: Any) -> str:
    return f"{_label(workflow)}, step {step_id}"


def _cfg(step: dict[str, Any]) -> dict[str, Any]:
    cfg = step.get("config")
    return {k: v for k, v in cfg.items() if v is not None} if isinstance(cfg, dict) else {}


def _steps_by_id(workflow: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {s["id"]: s for s in workflow["steps"] if isinstance(s, dict) and s.get("id")}


def _links(step: dict[str, Any]) -> list[str]:
    return [step[link] for link in LINKS if step.get(link)]


def _reachable(steps: dict[str, dict], entry_id: str) -> set[str]:
    seen: set[str] = set()
    stack = [entry_id]
    while stack:
        step_id = stack.pop()
        if step_id in seen or step_id not in steps:
            continue
        seen.add(step_id)
        stack.extend(_links(steps[step_id]))
    return seen


def _has_loop(steps: dict[str, dict], entry_id: str) -> bool:
    def visit(step_id: str, path: set[str]) -> bool:
        if step_id in path:
            return True
        if step_id not in steps:
            return False
        return any(visit(target, path | {step_id}) for target in _links(steps[step_id]))

    return visit(entry_id, set())


def _next_message_without_wait(steps: dict[str, dict], message: dict[str, Any]) -> str | None:
    """First Communication reachable from `message` without passing a Wait."""
    stack, seen = _links(message), set()
    while stack:
        step_id = stack.pop()
        if step_id in seen or step_id not in steps:
            continue
        seen.add(step_id)
        step = steps[step_id]
        if step.get("kind") == "Communication":
            return step_id
        if step.get("kind") != "Wait":
            stack.extend(_links(step))
    return None


def _days(step: dict[str, Any]) -> float:
    """Delay a step adds on its path (only "After" timings move time forward)."""
    cfg = _cfg(step)
    if cfg.get("direction") == "Before" or not isinstance(cfg.get("amount"), (int, float)):
        return 0.0
    return cfg["amount"] * DAYS_PER_UNIT.get(cfg.get("unit"), 1)


def _crowded_messages(steps: dict[str, dict], entry_id: str, cap: int) -> list[str]:
    """Message ids on the first path that sends more than `cap` messages in any 7 days."""

    def walk(step_id: str, day: float, sent: list[tuple[float, str]]) -> list[str]:
        step = steps[step_id]
        day += _days(step)
        if step.get("kind") == "Communication":
            sent = sent + [(day, step_id)]
            window = [sid for d, sid in sent if day - d < 7]
            if len(window) > cap:
                return window
        for target in _links(step):
            found = walk(target, day, sent)
            if found:
                return found
        return []

    return walk(entry_id, 0.0, [])
