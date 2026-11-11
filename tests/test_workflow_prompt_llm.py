from unittest.mock import patch

import httpx
import pytest

from app.core.exceptions import ServiceUnavailableError
from app.modules.payflow.services.strategy_service import (
    CASE_ACTIONS,
    CONDITION_VALUES,
    MESSAGE_PURPOSES,
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
    assert "PayFlow can contact this client's customers by: Email" in system
    catalog = system.split("## Examples")[0]
    assert "sms-link" not in catalog and "em-reminder" not in catalog  # no templates: contact steps have no channel
    assert '"segment_id": "A"' in user and '"segment_id": "B"' in user
    assert "Portfolio: Residential" in user and "First Party" in user


def test_examples_only_use_catalog_values():
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
            if step["kind"] == "Communication":  # contact steps: no channel / template, the worker picks
                assert "channel" not in cfg and "template_id" not in cfg and cfg["purpose"] in MESSAGE_PURPOSES
            if step["kind"] == "Condition":
                assert cfg["value"] in CONDITION_VALUES[cfg["attribute"]]
            if "action" in cfg:
                assert cfg["action"] in CASE_ACTIONS + PAYMENT_ACTIONS
            if "outcome" in cfg:
                assert cfg["outcome"] in OUTCOMES


def test_prompt_says_stop_when_the_customer_pays_in_full_pays_part_or_sets_up_a_plan():
    system = build_messages(CLIENT, "Residential", SEGMENTS, design_limits(CLIENT, {"system": [], "client": []}))[0][
        "content"
    ]
    rule_text = system.split("## Stop when the customer pays")[1].split("## Strategy guidance")[0]
    assert "pay in full, pay part, or set up a payment plan" in rule_text
    # What the worker's Payment Status means (workflow_condition_runtime.payment_status).
    assert '"Partial Payment" = paid part\nsince it started (a part payment from before does not count)' in rule_text
    assert '"Unpaid" = an instalment\nis missed and "Payment Plan Active" = the plan is on track' in rule_text
    assert "In Active and Promise to Pay workflows" in rule_text
    assert '"Payment Status Not Equals Unpaid"' in rule_text  # any payment or plan? yes = stop
    # Stop branch: paid in full / on a plan / part paid are kept apart.
    assert 'Condition "Payment Status Equals Paid In Full" (paid in full?): yes → Outcome "Paid In Full"' in rule_text
    assert 'Condition "Payment Status Equals Payment Plan Active" (on a plan?): yes → Outcome "Payment Plan Active"' in (
        rule_text
    )
    assert "no (paid part only) → Human Review to decide on the rest" in rule_text
    assert 'In every workflow, Outcome "Paid In Full" closes the case and clears the balance' in rule_text
    assert 'never use "Payment Status Not Equals Unpaid" or\n"Payment Status Equals Payment Plan Active"' in rule_text
    assert 'Condition "Payment Status Equals Unpaid" (instalment missed?)' in rule_text  # instead, with no loops
    assert 'no → Outcome\n"Payment Plan Active"' in rule_text
    assert "Not Equals Partial Payment" not in rule_text  # the old branch sent plan customers to "Paid In Full"


def test_prompt_explains_how_payflow_times_waits_and_reads_customer_responses():
    system = build_messages(CLIENT, "Residential", SEGMENTS, design_limits(CLIENT, {"system": [], "client": []}))[0][
        "content"
    ]
    assert 'After the first message, time every Wait from "Previous Action"' in system
    assert "never from Case Received or Due Date: those dates are in the past for\nmost accounts" in system
    assert 'Only check Customer Response for "Dispute Raised"' in system


def test_examples_keep_paid_in_full_plans_and_part_payments_apart():
    for example in load_examples():
        steps = {s["id"]: s for s in example["steps"]}
        checks = [s for s in example["steps"] if s["config"].get("attribute") == "Payment Status"]
        assert checks and {(c["config"]["operator"], c["config"]["value"]) for c in checks} == {
            ("Not Equals", "Unpaid"),
            ("Equals", "Paid In Full"),
            ("Equals", "Payment Plan Active"),
        }, example["name"]
        paid_in_full = next(c for c in checks if c["config"]["value"] == "Paid In Full")
        assert steps[paid_in_full["yes"]]["config"]["outcome"] == "Paid In Full"
        on_a_plan = steps[paid_in_full["no"]]
        assert on_a_plan["config"]["value"] == "Payment Plan Active"
        assert steps[on_a_plan["yes"]]["config"]["outcome"] == "Payment Plan Active"
        assert steps[on_a_plan["no"]]["kind"] == "Human Review"


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


def test_output_schema_is_compact_and_limited_to_segments():
    schema = llm.build_output_schema(["A", "B"])
    _assert_closed_objects(schema)
    workflow = schema["properties"]["workflows"]["items"]
    assert workflow["properties"]["segment_id"]["enum"] == ["A", "B"]
    step = workflow["properties"]["steps"]["items"]
    assert step["required"] == ["id", "kind", "title", "config"]
    config = step["properties"]["config"]
    assert config["required"] == []  # unused fields are left out
    assert "channel" not in config["properties"] and "template_id" not in config["properties"]
    assert "Email Delivery" not in config["properties"]["attribute"]["enum"]  # the worker handles delivery
    assert "Previous Email" not in config["properties"]["reference_event"]["enum"]


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
    with patch.object(llm.settings, "LLM_PROVIDER", "groq"), patch.object(llm.settings, "GROQ_API_KEY", "test-key"):
        yield


@pytest.fixture()
def together_key():
    with patch.object(llm.settings, "LLM_PROVIDER", "together"), patch.object(
        llm.settings, "TOGETHER_API_KEY", "together-key"
    ):
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
    with patch.object(llm.httpx, "post", return_value=response), patch.object(llm.time, "sleep"):
        with pytest.raises(ServiceUnavailableError, match=message):
            llm.call_llm([], {})


def test_call_llm_network_error(groq_key):
    with patch.object(llm.httpx, "post", side_effect=httpx.ConnectError("down")) as post, \
            patch.object(llm.time, "sleep") as sleep:
        with pytest.raises(ServiceUnavailableError, match="could not be reached"):
            llm.call_llm([], {})
    assert post.call_count == len(llm.SERVER_ERROR_WAITS_SECONDS) + 1  # tried again before giving up
    assert [c.args[0] for c in sleep.call_args_list] == list(llm.SERVER_ERROR_WAITS_SECONDS)


@pytest.mark.parametrize(
    "first",
    [httpx.ReadTimeout("slow"), httpx.ConnectError("down"), FakeResponse(status_code=503), FakeResponse(status_code=500)],
    ids=["timeout", "connection error", "503", "500"],
)
def test_call_llm_tries_again_when_the_service_is_briefly_down(groq_key, first):
    with patch.object(llm.httpx, "post", side_effect=[first, FakeResponse()]) as post, \
            patch.object(llm.time, "sleep") as sleep:
        assert llm.call_llm([], {}) == {"workflows": []}
    assert post.call_count == 2
    sleep.assert_called_once_with(llm.SERVER_ERROR_WAITS_SECONDS[0])


def test_call_llm_gives_up_after_server_error_retries(groq_key):
    with patch.object(llm.httpx, "post", return_value=FakeResponse(status_code=502)) as post, \
            patch.object(llm.time, "sleep"):
        with pytest.raises(ServiceUnavailableError, match=r"AI service error \(502\)"):
            llm.call_llm([], {})
    assert post.call_count == len(llm.SERVER_ERROR_WAITS_SECONDS) + 1


def test_call_llm_does_not_retry_a_bad_request(groq_key):
    """4xx (other than 429) will fail the same way again: no retry."""
    with patch.object(llm.httpx, "post", return_value=FakeResponse(status_code=400)) as post, \
            patch.object(llm.time, "sleep") as sleep:
        with pytest.raises(ServiceUnavailableError, match=r"AI service error \(400\)"):
            llm.call_llm([], {})
    assert post.call_count == 1 and not sleep.called


def test_call_llm_without_key():
    with patch.object(llm.settings, "LLM_PROVIDER", "groq"), patch.object(llm.settings, "GROQ_API_KEY", ""):
        with pytest.raises(ServiceUnavailableError, match="GROQ_API_KEY"):
            llm.call_llm([], {})
    with patch.object(llm.settings, "LLM_PROVIDER", "together"), patch.object(llm.settings, "TOGETHER_API_KEY", ""):
        with pytest.raises(ServiceUnavailableError, match="TOGETHER_API_KEY"):
            llm.call_llm([], {})


def test_call_llm_groq_sends_no_max_tokens(groq_key):
    """Groq's free tier counts max_tokens against its 8k tokens-per-minute limit."""
    with patch.object(llm.httpx, "post", return_value=FakeResponse()) as post:
        llm.call_llm([], {})
    assert post.call_args.args[0] == llm.GROQ_URL
    assert post.call_args.kwargs["json"]["model"] == llm.settings.GROQ_MODEL
    assert "max_tokens" not in post.call_args.kwargs["json"]


def test_call_llm_uses_together_when_chosen(together_key):
    with patch.object(llm.httpx, "post", return_value=FakeResponse()) as post:
        assert llm.call_llm([{"role": "user", "content": "hi"}], {}) == {"workflows": []}
    payload = post.call_args.kwargs["json"]
    assert post.call_args.args[0] == "https://api.together.ai/v1/chat/completions"
    assert post.call_args.kwargs["headers"]["Authorization"] == "Bearer together-key"
    assert payload["model"] == llm.settings.TOGETHER_MODEL
    assert payload["max_tokens"] == llm.settings.TOGETHER_MAX_TOKENS  # reasoning + JSON share this budget
    assert payload["reasoning_effort"] == llm.settings.GROQ_REASONING_EFFORT
    assert payload["response_format"]["type"] == "json_schema"


def test_call_llm_unknown_provider():
    with patch.object(llm.settings, "LLM_PROVIDER", "openrouter"):
        with pytest.raises(ServiceUnavailableError, match="Unknown LLM_PROVIDER 'openrouter'"):
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
    workflow = llm.build_output_schema(["A"])["properties"]["workflows"]["items"]
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


# --------------------------------------------------------------------------- review history (regenerate)


def test_review_history_lists_edits_rejects_and_notes_oldest_first():
    from app.modules.payflow.services.workflow_suggestion.prompt import build_review_history

    versions = [  # stored newest first
        {"note": "Too aggressive", "changes": [], "reviewer_note": "Too aggressive"},  # reject with a note
        {"note": "Review started by Sara", "changes": []},  # no feedback → skipped
        {"note": "Updated by Sara", "changes": ["Wait 3 days: timing changed"], "reviewer_note": "SMS too early"},
        {"note": "Proposed by PayFlow AI", "changes": []},  # no feedback → skipped
    ]
    assert build_review_history(versions).splitlines() == [
        "Review history (oldest first). Keep what reviewers changed unless the remarks say otherwise:",
        "- Updated by Sara. Changed: Wait 3 days: timing changed. Reason: SMS too early",
        "- Rejected. Reason: Too aggressive",
    ]


def test_review_history_is_empty_without_feedback_and_capped():
    from app.modules.payflow.services.workflow_suggestion.prompt import (
        MAX_HISTORY_ENTRIES,
        build_review_history,
        build_revision,
    )

    assert build_review_history([{"note": "Proposed by PayFlow AI", "changes": []}]) == ""
    assert "Review history" not in build_revision("A", {"steps": [], "versions": []}, "fewer SMS")

    edits = [{"note": f"Updated by Sara ({i})", "changes": ["x"]} for i in range(MAX_HISTORY_ENTRIES + 5)]
    lines = build_review_history(edits).splitlines()[1:]
    assert len(lines) == MAX_HISTORY_ENTRIES
    assert lines[-1].startswith("- Updated by Sara (0)")  # the newest entry is kept
