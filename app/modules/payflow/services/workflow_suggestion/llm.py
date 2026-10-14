"""LLM call (Groq, OpenAI-compatible) with a JSON schema built from the catalog."""

from __future__ import annotations

import json
import time
from typing import Any

import httpx

from app.core.config import settings
from app.core.exceptions import ServiceUnavailableError
from app.modules.payflow.services.strategy_service import (
    CASE_ACTIONS,
    CONDITION_ATTRIBUTES,
    CONDITION_OPERATORS,
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
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
RATE_LIMIT_RETRIES = 5
MAX_RATE_LIMIT_WAIT_SECONDS = 60


def _object(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": required,
        "properties": properties,
    }


def _enum(values: list[str]) -> dict[str, Any]:
    return {"type": "string", "enum": values}


def build_output_schema(segment_ids: list[str], channels: list[str]) -> dict[str, Any]:
    # Unused config fields and links are left out (not null) to keep replies short.
    config = _object(
        {
            "channel": _enum(channels),
            "purpose": _enum(MESSAGE_PURPOSES),
            "template_id": _enum([t["id"] for t in MESSAGE_TEMPLATES if t["channel"] in channels]),
            "reference_event": _enum(REFERENCE_EVENTS),
            "amount": {"type": "number"},
            "unit": _enum(TIME_UNITS),
            "direction": _enum(TIME_DIRECTIONS),
            "attribute": _enum(CONDITION_ATTRIBUTES),
            "operator": _enum(CONDITION_OPERATORS),
            "value": {"type": "string"},
            "action": _enum(CASE_ACTIONS + PAYMENT_ACTIONS),
            "outcome": _enum(OUTCOMES),
            "note": {"type": "string"},
        },
        required=[],
    )
    step = _object(
        {
            "id": {"type": "string"},
            "kind": _enum(STRATEGY_STEP_KINDS),
            "title": {"type": "string"},
            "config": config,
            "next": {"type": "string"},
            "yes": {"type": "string"},
            "no": {"type": "string"},
        },
        required=["id", "kind", "title", "config"],
    )
    text = {"type": "string"}
    reasoning = _object(
        {field: text for field in REASONING_FIELDS},
        required=[field for field in REASONING_FIELDS if field != "changes_made"],  # changes_made: regenerate only
    )
    workflow = _object(
        {
            "segment_id": _enum(segment_ids),
            "name": text,
            "summary": text,
            "reasoning": reasoning,
            "entry_node_id": text,
            "steps": {"type": "array", "items": step},
        },
        required=["segment_id", "name", "summary", "reasoning", "entry_node_id", "steps"],
    )
    return _object({"workflows": {"type": "array", "items": workflow}}, required=["workflows"])


def call_llm(messages: list[dict[str, str]], schema: dict[str, Any]) -> dict[str, Any]:
    if not settings.GROQ_API_KEY:
        raise ServiceUnavailableError("AI workflow suggestions are not configured (GROQ_API_KEY is missing)")
    payload = {
        "model": settings.GROQ_MODEL,
        "messages": messages,
        "temperature": 0.3,
        "reasoning_effort": settings.GROQ_REASONING_EFFORT,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "workflow_suggestions", "strict": False, "schema": schema},
        },
    }
    response = _post_with_rate_limit_wait(payload)
    if response.status_code != 200:
        raise ServiceUnavailableError(f"AI service error ({response.status_code}): {_error_message(response)}")
    choice = response.json()["choices"][0]
    if choice.get("finish_reason") == "length":
        raise ServiceUnavailableError("AI response was cut off before it finished")
    try:
        return json.loads(choice["message"]["content"])
    except (TypeError, ValueError) as exc:
        raise ServiceUnavailableError("AI service returned invalid JSON") from exc


def _post_with_rate_limit_wait(payload: dict[str, Any]) -> httpx.Response:
    """POST to Groq; on 429 (rate limit) wait as told by `retry-after`, then retry."""
    for attempt in range(RATE_LIMIT_RETRIES + 1):
        try:
            response = httpx.post(
                GROQ_URL,
                json=payload,
                headers={"Authorization": f"Bearer {settings.GROQ_API_KEY}"},
                timeout=settings.GROQ_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as exc:
            raise ServiceUnavailableError("AI service could not be reached") from exc
        if response.status_code != 429 or attempt == RATE_LIMIT_RETRIES:
            return response
        time.sleep(_retry_after_seconds(response))
    return response


def _retry_after_seconds(response: httpx.Response) -> float:
    try:
        seconds = float(response.headers.get("retry-after", 10))
    except ValueError:
        seconds = 10.0
    return min(max(seconds, 1.0), MAX_RATE_LIMIT_WAIT_SECONDS)


def _error_message(response: httpx.Response) -> str:
    try:
        return response.json()["error"]["message"]
    except (ValueError, KeyError, TypeError):
        return response.text[:200]
