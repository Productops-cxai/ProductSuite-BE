"""Plain-language AI explanation of a compliance rule (Lovable RuleAiExplanation parity)."""

from __future__ import annotations

import json
import logging
from typing import Any

from app.core.exceptions import AppError, ServiceUnavailableError
from app.modules.payflow.services.workflow_suggestion import message_writer

logger = logging.getLogger(__name__)

_SYSTEM = (
    "You explain debt-collection communication compliance rules to non-technical operations staff. "
    "Use plain, short sentences. Fill exactly three fields: what_it_is, when_it_applies, what_happens. "
    "1-3 sentences each. No legal advice."
)

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["what_it_is", "when_it_applies", "what_happens"],
    "properties": {
        "what_it_is": {"type": "string"},
        "when_it_applies": {"type": "string"},
        "what_happens": {"type": "string"},
    },
}

# Fields that are useful for the model; omit ids / audit noise.
_PROMPT_KEYS = (
    "name",
    "description",
    "category",
    "rule_type",
    "enforcement",
    "regulatory_authority",
    "legal_reference",
    "country",
    "state",
    "region",
    "city",
    "postal",
    "channel",
    "collector_type",
    "debt_type",
    "logic",
    "conditions",
    "settings",
    "action",
    "override_policy",
    "priority",
    "effective_from",
    "effective_until",
    "status",
)


def _payload(rule: dict[str, Any]) -> dict[str, Any]:
    return {key: rule.get(key) for key in _PROMPT_KEYS if rule.get(key) not in (None, "", [], {})}


def explain_rule(rule: dict[str, Any]) -> dict[str, str]:
    """Return ``{text}`` with Lovable-style section headings, or raise AppError."""
    try:
        reply = message_writer.call_llm(
            [
                {"role": "system", "content": _SYSTEM},
                {
                    "role": "user",
                    "content": f"Explain this rule configuration:\n{json.dumps(_payload(rule), ensure_ascii=False)}",
                },
            ],
            _SCHEMA,
            quick=True,
        )
    except AppError:
        raise
    except Exception as exc:
        logger.info("compliance rule explain: AI unavailable (%s)", exc)
        raise ServiceUnavailableError("Could not generate an explanation right now.") from exc

    what = str((reply or {}).get("what_it_is") or "").strip()
    when = str((reply or {}).get("when_it_applies") or "").strip()
    happens = str((reply or {}).get("what_happens") or "").strip()
    if not (what or when or happens):
        raise ServiceUnavailableError("No explanation returned.")

    lines = []
    if what:
        lines.extend(["What it is", what])
    if when:
        lines.extend(["When it applies", when])
    if happens:
        lines.extend(["What happens", happens])
    return {"text": "\n".join(lines)}
