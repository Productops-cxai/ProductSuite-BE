"""LLM call (Groq or Together AI, both OpenAI-compatible) with a JSON schema built from the catalog."""

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
    OUTCOMES,
    PAYMENT_ACTIONS,
    REFERENCE_EVENTS,
    STRATEGY_STEP_KINDS,
    TIME_DIRECTIONS,
    TIME_UNITS,
)
from app.modules.payflow.services.workflow_suggestion.config import (
    CHANNEL_EVENTS,
    CONTACT_ATTRIBUTES,
    REASONING_FIELDS,
)

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
TOGETHER_URL = "https://api.together.ai/v1/chat/completions"
# LLM_PROVIDER → where to call and which settings hold its key, model and max_tokens.
# Groq gets no max_tokens: its free tier counts it against the 8k tokens-per-minute limit.
PROVIDERS: dict[str, dict[str, str | None]] = {
    "groq": {"url": GROQ_URL, "key": "GROQ_API_KEY", "model": "GROQ_MODEL", "max_tokens": None},
    "together": {
        "url": TOGETHER_URL,
        "key": "TOGETHER_API_KEY",
        "model": "TOGETHER_MODEL",
        "max_tokens": "TOGETHER_MAX_TOKENS",
    },
}
RATE_LIMIT_RETRIES = 5
MAX_RATE_LIMIT_WAIT_SECONDS = 60
# The service is briefly down (timeout, connection error, 5xx): wait this long, then try again —
# one wait per retry. Without it one hiccup failed the whole Suggest (all-or-nothing).
SERVER_ERROR_WAITS_SECONDS = (2, 5)


def _object(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": required,
        "properties": properties,
    }


def _enum(values: list[str]) -> dict[str, Any]:
    return {"type": "string", "enum": values}


def build_output_schema(segment_ids: list[str]) -> dict[str, Any]:
    # Unused config fields and links are left out (not null) to keep replies short.
    # No channel / template: a Communication step contacts the customer, the worker picks email or SMS.
    # Keys in the order the prompt and examples write them (amount, unit, direction, reference_event):
    # Together AI only lets the model write keys in schema order, so a later reference_event was dropped.
    config = _object(
        {
            "purpose": _enum(MESSAGE_PURPOSES),
            "amount": {"type": "number"},
            "unit": _enum(TIME_UNITS),
            "direction": _enum(TIME_DIRECTIONS),
            "reference_event": _enum([e for e in REFERENCE_EVENTS if e not in CHANNEL_EVENTS]),
            "attribute": _enum([a for a in CONDITION_ATTRIBUTES if a not in CONTACT_ATTRIBUTES]),
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


def build_message_schema(step_ids: list[str]) -> dict[str, Any]:
    """Reply of the message writer, per step: subject (emails) + body; sms_body for a step without a channel."""
    text = {"type": "string"}
    message = _object(
        {"step_id": _enum(step_ids), "subject": text, "body": text, "sms_body": text}, required=["step_id", "body"]
    )
    return _object({"messages": {"type": "array", "items": message}}, required=["messages"])


def call_llm(messages: list[dict[str, str]], schema: dict[str, Any], *, quick: bool = False) -> dict[str, Any]:
    """`quick`: for a call made while a workflow step or a page is waiting (one case, a short
    answer): no waiting out a rate limit and one retry only - the caller has a fallback."""
    provider = _provider()
    api_key = getattr(settings, provider["key"])
    if not api_key:
        raise ServiceUnavailableError(f"AI workflow suggestions are not configured ({provider['key']} is missing)")
    payload = {
        "model": getattr(settings, provider["model"]),
        "messages": messages,
        "temperature": 0.3,
        "reasoning_effort": settings.GROQ_REASONING_EFFORT,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "workflow_suggestions", "strict": False, "schema": schema},
        },
    }
    if provider["max_tokens"]:
        payload["max_tokens"] = getattr(settings, provider["max_tokens"])
    response = _post_with_retries(
        provider["url"],
        api_key,
        payload,
        rate_limit_retries=0 if quick else RATE_LIMIT_RETRIES,
        server_error_waits=SERVER_ERROR_WAITS_SECONDS[:1] if quick else SERVER_ERROR_WAITS_SECONDS,
    )
    if response.status_code != 200:
        raise ServiceUnavailableError(f"AI service error ({response.status_code}): {_error_message(response)}")
    choice = response.json()["choices"][0]
    if choice.get("finish_reason") == "length":
        raise ServiceUnavailableError("AI response was cut off before it finished")
    try:
        return json.loads(choice["message"]["content"])
    except (TypeError, ValueError) as exc:
        raise ServiceUnavailableError("AI service returned invalid JSON") from exc


def _provider() -> dict[str, str | None]:
    name = (settings.LLM_PROVIDER or "groq").strip().lower()
    if name not in PROVIDERS:
        raise ServiceUnavailableError(f"Unknown LLM_PROVIDER '{name}' (use {' or '.join(PROVIDERS)})")
    return PROVIDERS[name]


def _post_with_retries(
    url: str,
    api_key: str,
    payload: dict[str, Any],
    *,
    rate_limit_retries: int = RATE_LIMIT_RETRIES,
    server_error_waits: tuple[int, ...] = SERVER_ERROR_WAITS_SECONDS,
) -> httpx.Response:
    """POST to the AI service and retry what is temporary.

    429 (rate limit): wait as told by `retry-after`, up to `rate_limit_retries` times.
    Timeout, connection error or 5xx: wait `server_error_waits`, one wait per retry.
    """
    rate_limited = server_errors = 0
    while True:
        try:
            response = httpx.post(
                url,
                json=payload,
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=settings.GROQ_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as exc:  # timeouts and connection errors
            if server_errors == len(server_error_waits):
                raise ServiceUnavailableError("AI service could not be reached") from exc
            time.sleep(server_error_waits[server_errors])
            server_errors += 1
            continue
        if response.status_code == 429 and rate_limited < rate_limit_retries:
            time.sleep(_retry_after_seconds(response))
            rate_limited += 1
        elif response.status_code >= 500 and server_errors < len(server_error_waits):
            time.sleep(server_error_waits[server_errors])
            server_errors += 1
        else:
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
