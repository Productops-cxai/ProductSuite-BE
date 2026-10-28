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

# Regenerate sends at most this many review-history entries (keeps prompts small on the free tier).
MAX_HISTORY_ENTRIES = 10

# Email / SMS text on Communication steps — written separately (message_writer.py), not part of
# a workflow's design, so it stays out of the workflow prompts (examples, revisions).
MESSAGE_TEXT_FIELDS = ("subject", "body", "tone")

# Recorded consent counts only "Yes", but the worker also emails accounts whose consent is unknown
# (files without a consent column) — shown to the AI, it made every workflow SMS-only. The AI reads
# email_allowed_pct ("Email allowed" on saved workflows) instead. Suggest stats + Regenerate labels.
HIDDEN_STATS = {"email_consent_pct", "Email consent"}

# Kept next to a client's own examples: the generic example with real branches (bounce → SMS, dispute → hold).
BRANCHING_EXAMPLE = "Mid-Stage Channel-Aware Follow-Up"

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
    revision: str | None = None,
    client_examples: list[dict[str, Any]] | None = None,
    lessons: list[str] | None = None,
) -> list[dict[str, str]]:
    """`feedback` (on a retry): [{"segment", "workflow", "errors"}] from the validator.
    `revision` (on regenerate): the current workflow + reviewer remarks, from build_revision.
    `client_examples` / `lessons` (on suggest): learned from this client's past reviews (learning.py)."""
    user_prompt = build_user_prompt(client, portfolio_name, segments)
    if revision:
        user_prompt += "\n\n" + revision
    if feedback:
        user_prompt += "\n\n" + build_feedback(feedback)
    return [
        {"role": "system", "content": build_system_prompt(limits, client_examples, lessons)},
        {"role": "user", "content": user_prompt},
    ]


def build_feedback(feedback: list[dict[str, Any]]) -> str:
    parts = ["Your previous workflows for these segments had problems. Fix every problem and return corrected workflows:"]
    for item in feedback:
        if item["workflow"]:
            parts.append(json.dumps(item["workflow"], ensure_ascii=False))
        parts.append("Problems:\n" + "\n".join(f"- {error}" for error in item["errors"]))
    return "\n".join(parts)


def build_revision(
    segment_id: str,
    workflow: dict[str, Any],
    remarks: str,
    versions: list[dict[str, Any]] | None = None,
) -> str:
    """Ask for an improved version of an existing workflow, guided by a reviewer's remarks
    and the review history (what reviewers changed and why).

    `versions`: history entries to send, newest first — default the workflow's own;
    Regenerate also passes those of the workflows it was regenerated from."""
    current = {
        "name": workflow.get("name"),
        "summary": workflow.get("summary"),
        "entry_node_id": workflow.get("entry_node_id"),
        "steps": [compact_step(step) for step in workflow.get("steps") or []],
    }
    parts = [
        f"Revise the current workflow for segment_id \"{segment_id}\" using the reviewer's remarks. "
        "Keep what works and change what the remarks ask for. Return exactly one workflow, and fill "
        "reasoning.changes_made with what you changed and how it addresses the remarks.",
        f"Current workflow: {json.dumps(current, ensure_ascii=False)}",
    ]
    history = build_review_history(versions if versions is not None else workflow.get("versions") or [])
    if history:
        parts.append(history)
    parts.append(f"Reviewer remarks: {remarks}")
    return "\n".join(parts)


def build_review_history(versions: list[dict[str, Any]]) -> str:
    """Reviewer feedback from a workflow's history, oldest first — "" when there is none.

    Only entries with a change list or a reviewer_note are used (the last MAX_HISTORY_ENTRIES).
    """
    lines = [_history_line(entry) for entry in reversed(versions) if isinstance(entry, dict)]  # stored newest first
    lines = [line for line in lines if line][-MAX_HISTORY_ENTRIES:]
    if not lines:
        return ""
    return "Review history (oldest first). Keep what reviewers changed unless the remarks say otherwise:\n" + "\n".join(
        lines
    )


def _history_line(entry: dict[str, Any]) -> str | None:
    reason = (entry.get("reviewer_note") or "").strip()
    changes = [str(change) for change in entry.get("changes") or []]
    if not (reason or changes):
        return None  # plain status moves (review started, activated, …) carry no feedback
    note = (entry.get("note") or "").strip()
    # Reject stores the reviewer's text as its note, so a note equal to the reason is a rejection.
    parts = ["Rejected" if reason and note == reason else note]
    if changes:
        parts.append("Changed: " + "; ".join(changes))
    if reason:
        parts.append(f"Reason: {reason}")
    return "- " + ". ".join(parts)


def compact_step(step: dict[str, Any]) -> dict[str, Any]:
    """A stored step without its empty fields or message text — the short form the AI reads and writes."""
    compact = {key: step[key] for key in ("id", "kind", "title") if step.get(key)}
    compact["config"] = {
        k: v for k, v in (step.get("config") or {}).items() if v is not None and k not in MESSAGE_TEXT_FIELDS
    }
    compact.update({link: step[link] for link in ("next", "yes", "no") if step.get(link)})
    return compact


def build_system_prompt(
    limits: dict[str, Any],
    client_examples: list[dict[str, Any]] | None = None,
    lessons: list[str] | None = None,
) -> str:
    """Without client_examples / lessons the prompt is exactly the generic one."""
    channels = limits["channels"]
    templates = [t for t in MESSAGE_TEMPLATES if t["channel"] in channels]
    template_lines = "\n".join(f"  {t['id']} — {t['channel']} — {t['purpose']}" for t in templates)
    condition_lines = "\n".join(
        f"  {attribute}: {', '.join(values)}" for attribute, values in CONDITION_VALUES.items()
    )
    weekly_cap = limits["max_messages_per_7_days"]
    weekly_line = f"\n- At most {weekly_cap} messages in any 7 days." if weekly_cap is not None else ""
    generic = load_examples()
    if client_examples:
        # The client's own (similar) workflows take generic slots; free slots keep generic
        # examples, and the branching example always stays — same number of examples as before.
        branching = [e for e in generic if e["name"] == BRANCHING_EXAMPLE]
        free_slots = max(0, len(generic) - len(branching) - len(client_examples))
        fillers = [e for e in generic if e["name"] != BRANCHING_EXAMPLE][:free_slots]
        examples_intro = (
            f"The first {len(client_examples)} are this client's own workflows, approved by its reviewers: "
            "learn from their choices, but design for each segment's own situation and stats.\n"
        )
        example_list = client_examples + fillers + branching
    else:
        examples_intro, example_list = "", generic
    examples = examples_intro + "\n".join(json.dumps(e, ensure_ascii=False) for e in example_list)
    examples += _lessons_section(lessons)

    return f"""You design debt-collection workflows for PayFlow, a collections platform.
You receive a client, one of its portfolios, and segments of that portfolio's accounts.
Design exactly one workflow per segment_id, tailored to that segment's situation and stats.
Reply with JSON only, matching the provided schema.

## How a workflow is built
A workflow is a flowchart of steps.
- Every step has a unique id ("s1", "s2", ...), a kind, a short title and a config.
- entry_node_id is the first step and must be a Trigger.
- A step points to the following step with "next". A Condition uses "yes" and "no" instead.
- The yes and no paths may continue with different steps and join again later.
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
- Use the stats: few accounts that can get email (low email allowed) = favour SMS; few mobiles = favour Email;
  neither likely to work = request a contact details update.
- Branch on what happens, not only "Paid?": Email / SMS Delivery failed or bounced = switch channel;
  Payment Link clicked but unpaid = gentle follow-up; Customer Response = Dispute Raised = hold contact;
  high Outstanding Balance = Human Review.
- Be respectful and compliant: no threats, no pressure tactics.
- name: short and descriptive. summary: 1-2 sentences.
- reasoning (each field 1-2 sentences, quote the segment's numbers): segment_fit = why this workflow suits
  this segment; channels = why these channels; timing = why these waits and timings;
  escalation = when and why it hands over (Human Review, hold, escalation).

## Examples (illustrative only, do not copy)
{examples}"""


def _lessons_section(lessons: list[str] | None) -> str:
    """This client's reviewer feedback — explicitly guidance, never hard rules."""
    if not lessons:
        return ""
    return (
        "\n\n## Lessons from this client's past reviews (guidance, not rules)\n"
        "This client's reviewers edited, rejected or approved earlier workflows for the reasons below. "
        "Treat them as the client's preferences, not as hard rules: follow a lesson where it fits a segment's "
        "situation and stats, and depart from it where it does not (then say why in reasoning). "
        "Only the Limits and Allowed values above are hard rules.\n" + "\n".join(lessons)
    )


def _design_stats(stats: dict[str, Any]) -> dict[str, Any]:
    """Segment stats for the design AI, without the recorded-consent number."""
    return {key: value for key, value in stats.items() if key not in HIDDEN_STATS}


def build_user_prompt(client: dict[str, Any], portfolio_name: str, segments: list[dict[str, Any]]) -> str:
    client_type = _CLIENT_TYPES.get(client.get("client_type") or "", client.get("client_type") or "Unknown")
    ai_mode = _AI_MODES.get(client.get("ai_mode") or "", client.get("ai_mode") or "Unknown")
    segment_lines = "\n".join(
        json.dumps(
            {"segment_id": s["segment_id"], "segment": s["label"], "stats": _design_stats(s["stats"])},
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
