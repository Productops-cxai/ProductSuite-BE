"""Build the LLM prompt: catalog values, design limits, guidance, examples, segments.

Rules are not in the prompt — code turns them into design limits (rules.py).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.modules.payflow.services.strategy_service import (
    CASE_ACTIONS,
    CONDITION_OPERATORS,
    CONDITION_VALUES,
    MESSAGE_TEMPLATES,
    OUTCOMES,
    PAYMENT_ACTIONS,
    REFERENCE_EVENTS,
    TIME_DIRECTIONS,
    TIME_UNITS,
)

EXAMPLES_PATH = Path(__file__).parent / "prompts" / "workflow_examples.json"

# Steps allowed to end a path (no `next`): a final outcome, or a hand-over.
END_STEP_KINDS = ("Outcome", "Human Review", "Case Action")

_CLIENT_TYPES = {"first_party": "First Party", "third_party": "Third Party"}
_AI_MODES = {"autopilot": "Autopilot", "supervised_ai": "Supervised AI"}


def load_examples() -> list[dict[str, Any]]:
    return json.loads(EXAMPLES_PATH.read_text(encoding="utf-8"))


def build_messages(
    client: dict[str, Any],
    portfolio_name: str,
    segments: list[dict[str, Any]],
    limits: dict[str, Any],
    feedback: list[dict[str, Any]] | None = None,
) -> list[dict[str, str]]:
    """`feedback` (on a retry): [{"segment", "workflow", "errors"}] from the validator."""
    user_prompt = build_user_prompt(client, portfolio_name, segments)
    if feedback:
        user_prompt += "\n\n" + build_feedback(feedback)
    return [
        {"role": "system", "content": build_system_prompt(limits)},
        {"role": "user", "content": user_prompt},
    ]


def build_feedback(feedback: list[dict[str, Any]]) -> str:
    parts = ["Your previous workflows for these segments had problems. Fix every problem and return corrected workflows:"]
    for item in feedback:
        if item["workflow"]:
            parts.append(json.dumps(item["workflow"], ensure_ascii=False))
        parts.append("Problems:\n" + "\n".join(f"- {error}" for error in item["errors"]))
    return "\n".join(parts)


def build_system_prompt(limits: dict[str, Any]) -> str:
    channels = limits["channels"]
    templates = [t for t in MESSAGE_TEMPLATES if t["channel"] in channels]
    template_lines = "\n".join(f"  {t['id']} — {t['channel']} — {t['purpose']}" for t in templates)
    condition_lines = "\n".join(
        f"  {attribute}: {', '.join(values)}" for attribute, values in CONDITION_VALUES.items()
    )
    weekly_cap = limits["max_messages_per_7_days"]
    weekly_line = f"\n- At most {weekly_cap} messages in any 7 days." if weekly_cap is not None else ""
    examples = "\n".join(json.dumps(e, ensure_ascii=False) for e in load_examples())

    return f"""You design debt-collection workflows for PayFlow, a collections platform.
You receive a client, one of its portfolios, and segments of that portfolio's accounts.
Design exactly one workflow per segment_id, tailored to that segment's situation and stats.
Reply with JSON only, matching the provided schema.

## How a workflow is built
A workflow is a flowchart of steps.
- Every step has a unique id ("s1", "s2", ...), a kind, a short title and a config.
- entry_node_id is the first step and must be a Trigger.
- A step points to the following step with "next". A Condition uses "yes" and "no" instead.
- A step without "next" ends its path. Only {", ".join(END_STEP_KINDS)} steps may end a path.
- Every step must be reachable from the entry step. No loops.
- Leave out every config field and link that a step does not use.

## Step kinds and their config
- Trigger: reference_event (when the workflow starts).
- Communication: channel, purpose, template_id (template with the same channel and purpose), and timing.
- Wait: timing.
- Condition: attribute, operator, value (value from that attribute's allowed values).
- Payment Action: action (from payment actions).
- Case Action: action (from case actions).
- Human Review: note (what the supervisor should check).
- AI Reassessment: note (what to reassess).
- Outcome: outcome.
Timing = amount + unit + direction + reference_event, e.g. 3 Days After Due Date. Never "Day 3". amount 0 = immediately.

## Allowed values
Channels enabled for this client: {", ".join(channels) or "none (do not use Communication steps)"}
Message templates (id — channel — purpose):
{template_lines or "  none"}
Reference events: {", ".join(REFERENCE_EVENTS)}
Time units: {", ".join(TIME_UNITS)}
Directions: {", ".join(TIME_DIRECTIONS)}
Condition operators: {", ".join(CONDITION_OPERATORS)}
Condition attributes and allowed values:
{condition_lines}
Payment actions: {", ".join(PAYMENT_ACTIONS)}
Case actions: {", ".join(CASE_ACTIONS)}
Outcomes: {", ".join(OUTCOMES)}

## Limits
- Between {limits["min_steps"]} and {limits["max_steps"]} steps per workflow.
- At most {limits["max_communications"]} Communication steps per workflow.{weekly_line}
- Put a Wait between two Communication steps.

## Strategy guidance
- Match intensity to the stage: early stage = friendly reminders and an easy way to pay;
  later stages = firmer reminders and payment plan offers, then Human Review before escalating.
- Promise to Pay = remind around the Promise-to-Pay Date, check payment, follow up if broken.
- Payment Plan = Payment Plan Reminders and checks that payments continue.
- Partly paid = acknowledge progress, offer a payment plan for the rest.
- Use the stats: low email consent or few emails = favour SMS; few mobiles = favour Email;
  neither likely to work = request a contact details update.
- Be respectful and compliant: no threats, no pressure tactics.
- name: short and descriptive. summary: 1-2 sentences. rationale: why this fits the segment's stats.

## Examples (illustrative only, do not copy)
{examples}"""


def build_user_prompt(client: dict[str, Any], portfolio_name: str, segments: list[dict[str, Any]]) -> str:
    client_type = _CLIENT_TYPES.get(client.get("client_type") or "", client.get("client_type") or "Unknown")
    ai_mode = _AI_MODES.get(client.get("ai_mode") or "", client.get("ai_mode") or "Unknown")
    segment_lines = "\n".join(
        json.dumps(
            {"segment_id": s["segment_id"], "segment": s["label"], "stats": s["stats"]},
            ensure_ascii=False,
        )
        for s in segments
    )
    return (
        f"Client: {client['name']} ({client_type}, AI mode: {ai_mode}, "
        f"currency: {client.get('currency') or 'CAD'}, language: {client.get('language') or 'English'})\n"
        f"Portfolio: {portfolio_name}\n"
        f"Segments - design one workflow for each segment_id:\n"
        f"{segment_lines}"
    )
