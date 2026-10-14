from unittest.mock import patch

import httpx
import pytest

from app.core.exceptions import ServiceUnavailableError
from app.modules.payflow.services.strategy_service import (
    CASE_ACTIONS,
    CONDITION_VALUES,
    MESSAGE_TEMPLATES,
    OUTCOMES,
    PAYMENT_ACTIONS,
    REFERENCE_EVENTS,
    STRATEGY_STEP_KINDS,
)
from app.modules.payflow.services.workflow_suggestion import llm
from app.modules.payflow.services.workflow_suggestion.prompt import (
    END_STEP_KINDS,
    build_messages,
    load_examples,
)
from app.modules.payflow.services.workflow_suggestion.rules import design_limits, governing_rules

CLIENT = {"name": "Northstar", "client_type": "first_party", "ai_mode": "supervised_ai", "channels": ["Email"]}
SEGMENTS = [
    {"segment_id": "A", "label": "Active · 1 – 29 days past due · Never contacted · All balances",
     "stats": {"accounts": 30}},
    {"segment_id": "B", "label": "Promise to Pay · All stages · Any contact history · All balances",
     "stats": {"accounts": 25}},
]


def rule(action, field, operator, value, logic="ALL", extra_conditions=()):
    conditions = [{"field": field, "operator": operator, "value": value}, *extra_conditions]
    return {"name": "r", "action": action, "logic": logic, "conditions": conditions}


def test_design_limits_channels_follow_client():
    limits = design_limits(CLIENT, {"system": [], "client": []})
    assert limits["channels"] == ["Email"]
    assert limits["max_messages_per_7_days"] is None


def test_weekly_message_cap_from_prevent_communication_rules():
    rules = {
        "system": [rule("Prevent Communication", "Messages Sent (7 days)", "Greater Than or Equal To", "4")],
        "client": [rule("Prevent Communication", "Messages Sent (7 days)", "Greater Than", "5")],
    }
    assert design_limits(CLIENT, rules)["max_messages_per_7_days"] == 3


def test_conditional_or_other_rules_do_not_cap_messages():
    other = {"field": "Outstanding Balance", "operator": "Greater Than", "value": "100"}
    rules = {
        "system": [
            rule("Prevent Communication", "Messages Sent (7 days)", "Greater Than or Equal To", "2",
                 extra_conditions=[other]),  # ALL + another condition → only sometimes
            rule("Require Human Review", "Messages Sent (7 days)", "Greater Than or Equal To", "2"),
        ],
        "client": [],
    }
    assert design_limits(CLIENT, rules)["max_messages_per_7_days"] is None


def test_prompt_has_limits_catalog_and_segments_but_no_rule_text():
    limits = design_limits(CLIENT, {"system": [], "client": []})
    limits["max_messages_per_7_days"] = 3
    system, user = (m["content"] for m in build_messages(CLIENT, "Residential", SEGMENTS, limits))
    assert "At most 3 messages in any 7 days" in system
    assert "Channels enabled for this client: Email" in system
    assert "sms-link" not in system.split("## Examples")[0]  # SMS templates hidden when SMS is off
    assert '"segment_id": "A"' in user and '"segment_id": "B"' in user
    assert "Portfolio: Residential" in user and "First Party" in user


def test_examples_only_use_catalog_values():
    templates = {t["id"]: t for t in MESSAGE_TEMPLATES}
    for example in load_examples():
        steps = {s["id"]: s for s in example["steps"]}
        assert steps[example["entry_node_id"]]["kind"] == "Trigger"
        for step in example["steps"]:
            cfg = step["config"]
            assert step["kind"] in STRATEGY_STEP_KINDS
            for link in ("next", "yes", "no"):
                assert step.get(link) is None or step[link] in steps
            if not (step.get("next") or step.get("yes")):
                assert step["kind"] in END_STEP_KINDS
            if "reference_event" in cfg:
                assert cfg["reference_event"] in REFERENCE_EVENTS
            if step["kind"] == "Communication":
                template = templates[cfg["template_id"]]
                assert (template["channel"], template["purpose"]) == (cfg["channel"], cfg["purpose"])
            if step["kind"] == "Condition":
                assert cfg["value"] in CONDITION_VALUES[cfg["attribute"]]
            if "action" in cfg:
                assert cfg["action"] in CASE_ACTIONS + PAYMENT_ACTIONS
            if "outcome" in cfg:
                assert cfg["outcome"] in OUTCOMES


def _assert_closed_objects(node):
    if isinstance(node, dict):
        if node.get("type") == "object":
            assert node["additionalProperties"] is False
            assert set(node["required"]) <= set(node["properties"])
        for value in node.values():
            _assert_closed_objects(value)
    elif isinstance(node, list):
        for value in node:
            _assert_closed_objects(value)


def test_output_schema_is_compact_and_limited_to_segments_and_channels():
    schema = llm.build_output_schema(["A", "B"], ["Email"])
    _assert_closed_objects(schema)
    workflow = schema["properties"]["workflows"]["items"]
    assert workflow["properties"]["segment_id"]["enum"] == ["A", "B"]
    step = workflow["properties"]["steps"]["items"]
    assert step["required"] == ["id", "kind", "title", "config"]
    config = step["properties"]["config"]
    assert config["required"] == []  # unused fields are left out
    assert config["properties"]["channel"]["enum"] == ["Email"]
    assert "sms-link" not in config["properties"]["template_id"]["enum"]


class FakeResponse:
    def __init__(self, status_code=200, content='{"workflows": []}', finish_reason="stop", headers=None):
        self.status_code = status_code
        self._body = {"choices": [{"finish_reason": finish_reason, "message": {"content": content}}]}
        self.text = "error"
        self.headers = headers or {}

    def json(self):
        if self.status_code != 200:
            return {"error": {"message": "boom"}}
        return self._body


@pytest.fixture()
def groq_key():
    with patch.object(llm.settings, "GROQ_API_KEY", "test-key"):
        yield


def test_call_llm_returns_parsed_json(groq_key):
    with patch.object(llm.httpx, "post", return_value=FakeResponse()) as post:
        assert llm.call_llm([{"role": "user", "content": "hi"}], {}) == {"workflows": []}
    assert post.call_args.kwargs["headers"]["Authorization"] == "Bearer test-key"


@pytest.mark.parametrize(
    "response, message",
    [
        (FakeResponse(status_code=500), r"AI service error \(500\): boom"),
        (FakeResponse(content="not json"), "invalid JSON"),
        (FakeResponse(finish_reason="length"), "cut off"),
    ],
)
def test_call_llm_errors(groq_key, response, message):
    with patch.object(llm.httpx, "post", return_value=response):
        with pytest.raises(ServiceUnavailableError, match=message):
            llm.call_llm([], {})


def test_call_llm_network_error(groq_key):
    with patch.object(llm.httpx, "post", side_effect=httpx.ConnectError("down")):
        with pytest.raises(ServiceUnavailableError, match="could not be reached"):
            llm.call_llm([], {})


def test_call_llm_without_key():
    with patch.object(llm.settings, "GROQ_API_KEY", ""):
        with pytest.raises(ServiceUnavailableError, match="GROQ_API_KEY"):
            llm.call_llm([], {})


def test_call_llm_sends_reasoning_effort(groq_key):
    with patch.object(llm.httpx, "post", return_value=FakeResponse()) as post:
        llm.call_llm([], {})
    assert post.call_args.kwargs["json"]["reasoning_effort"] == llm.settings.GROQ_REASONING_EFFORT


def test_call_llm_waits_on_rate_limit_then_succeeds(groq_key):
    responses = [FakeResponse(status_code=429, headers={"retry-after": "7"}), FakeResponse()]
    with patch.object(llm.httpx, "post", side_effect=responses), patch.object(llm.time, "sleep") as sleep:
        assert llm.call_llm([], {}) == {"workflows": []}
    sleep.assert_called_once_with(7.0)


def test_call_llm_gives_up_after_rate_limit_retries(groq_key):
    limited = FakeResponse(status_code=429, headers={"retry-after": "1"})
    with patch.object(llm.httpx, "post", return_value=limited) as post, patch.object(llm.time, "sleep"):
        with pytest.raises(ServiceUnavailableError, match="429"):
            llm.call_llm([], {})
    assert post.call_count == llm.RATE_LIMIT_RETRIES + 1


def test_retry_prompt_contains_previous_workflow_and_problems():
    limits = design_limits(CLIENT, {"system": [], "client": []})
    feedback = [{"segment": SEGMENTS[0], "workflow": {"segment_id": "A", "name": "Old"},
                 "errors": ["Workflow A, step s2: channel 'SMS' is not enabled for this client"]}]
    user = build_messages(CLIENT, "Residential", SEGMENTS[:1], limits, feedback)[1]["content"]
    assert "had problems" in user
    assert '"name": "Old"' in user
    assert "- Workflow A, step s2: channel 'SMS' is not enabled for this client" in user
    first_try = build_messages(CLIENT, "Residential", SEGMENTS[:1], limits)[1]["content"]
    assert "had problems" not in first_try


def test_governing_rules_text():
    rules = {
        "system": [rule("Prevent Communication", "Messages Sent (7 days)", "Greater Than or Equal To", "4")],
        "client": [],
    }
    rules["system"][0]["name"] = "Quiet Period Guard"
    lines = governing_rules({**CLIENT, "governance_rules": ["High Balance Human Review"]}, rules)
    assert lines == [
        "Quiet Period Guard (system rule): IF Messages Sent (7 days) greater than or equal to 4 "
        "THEN Prevent Communication",
        "High Balance Human Review (client governance setting)",
    ]


def test_schema_asks_for_structured_reasoning():
    workflow = llm.build_output_schema(["A"], ["Email"])["properties"]["workflows"]["items"]
    assert "reasoning" in workflow["required"] and "rationale" not in workflow["properties"]
    reasoning = workflow["properties"]["reasoning"]
    assert reasoning["required"] == ["segment_fit", "channels", "timing", "escalation"]
    assert "changes_made" in reasoning["properties"]  # optional: regenerate only


def test_prompt_explains_reasoning_and_revision_asks_for_changes():
    from app.modules.payflow.services.workflow_suggestion.prompt import build_revision

    limits = design_limits(CLIENT, {"system": [], "client": []})
    system = build_messages(CLIENT, "Residential", SEGMENTS, limits)[0]["content"]
    assert "reasoning (each field" in system and "segment_fit" in system
    assert "reasoning.changes_made" in build_revision("A", {"steps": []}, "fewer SMS")
