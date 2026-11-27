"""Adaptive workflows: writing the follow-ups (plan segment 5).

1. The follow-ups are written after the flowchart's messages, in an AI call of their own: when they cannot be
   written, the flowchart's messages are still saved and the follow-ups wait for Write messages.
2. A message may only offer the ways to pay the client's payment page has; a client without payment plans gets no
   "plan setup not finished" follow-up.
3. The follow-ups are translated in a call of their own too: when that fails, the language still counts for the
   other messages, and its customers get the planned message instead of a follow-up they cannot read.
"""

import pytest

from app.core.exceptions import ServiceUnavailableError
from app.infrastructure.database.models import PayflowClientModel
from app.modules.payflow.services.workflow_suggestion import message_writer
from app.modules.payflow.services.workflow_suggestion.languages import languages_line_for_steps
from app.modules.payflow.services.workflow_suggestion.message_validator import (
    FULL_OPTION,
    PART_OPTION,
    PLAN_OPTION,
    check_message,
)
from app.modules.payflow.services.workflow_suggestion.next_action import decide_next_action
from tests.test_adaptive_follow_ups import FOLLOW_UP_IDS, WITHOUT_PLAN_SETUP, _asked, _follow_ups, _suggest
from tests.test_adaptive_next_action import STEPS, _context
from tests.test_workflow_audience_messages import write_with
from tests.test_workflow_message_api import fake_message_ai, flowchart_messages, good_text
from tests.test_workflow_message_translation import TRANSLATE, _make_french, fake_writer
from tests.test_workflow_message_validator import EMAIL, brief
from tests.test_workflow_regenerate import admin, context  # noqa: F401 (fixture)
from tests.test_workflow_suggestion_api import portfolio  # noqa: F401 (fixture)


def _asks_follow_ups(messages):
    return any(step_id.startswith("fu_") for step_id in _asked(messages[1]["content"]))


# --------------------------------------------------------------------------- 1. their own call


def test_when_the_follow_ups_cannot_be_written_the_flowchart_is_still_saved(db_session, admin, portfolio):  # noqa: F811
    write, _ = fake_message_ai()

    def writer(messages, schema):
        if _asks_follow_ups(messages):
            raise ServiceUnavailableError("AI is busy")
        return write(messages, schema)

    workflow = _suggest(db_session, admin, portfolio, writer)
    assert all(config["body"] for config in flowchart_messages(workflow).values())
    assert list(_follow_ups(workflow)) == FOLLOW_UP_IDS
    assert not any(step["config"].get("body") for step in _follow_ups(workflow).values())
    assert "Not written yet, so never sent until written" in context(workflow)["Adaptive follow-ups"]


def test_asking_for_follow_ups_alone_still_fails_when_none_can_be_written(db_session, admin, portfolio):  # noqa: F811
    workflow = _suggest(db_session, admin, portfolio)
    broken, _ = fake_message_ai(fail=True)
    with pytest.raises(ServiceUnavailableError, match="could not write valid messages"):
        write_with(db_session, admin, workflow["id"], broken, step_id="fu_no_activity_repeat")


# --------------------------------------------------------------------------- 2. only the ways to pay offered


def _offers(text, options, **fields):
    email = {**EMAIL, "body": EMAIL["body"].replace("You can pay in full or set up a plan on your secure page",
                                                    text)}
    return check_message(email, {**brief(), "payment_options": options, **fields}, [])


def test_a_message_offers_only_the_ways_to_pay_the_client_has():
    plan = "You can set up a payment plan on your secure page"
    assert "offers a way to pay this client's payment page does not have" in _offers(plan, [FULL_OPTION])[0]
    assert "'set up a plan'" in _offers("You can set up a plan on your secure page", [FULL_OPTION, PART_OPTION])[0]
    assert "'pay part'" in _offers("You can pay part of it now on your secure page", [FULL_OPTION, PLAN_OPTION])[0]
    assert _offers(plan, [FULL_OPTION, PLAN_OPTION]) == []
    assert _offers(plan, [FULL_OPTION], plan_customer=True) == []  # already on a plan: it is about that plan
    assert _offers(plan, None) == []  # the client's options not known: not checked (e.g. a person's own text)
    thanks = "Thank you for your part payment. You can pay the rest on your secure page"
    assert _offers(thanks, [FULL_OPTION]) == []  # thanks them for what they did - offers nothing


def _without_plans(db_session, portfolio):  # noqa: F811
    client = db_session.get(PayflowClientModel, portfolio["client_id"])
    client.payment_settings = {"allow_payment_plan": False}
    db_session.commit()


def test_a_client_without_plans_gets_no_plan_setup_follow_up_and_no_plan_offers(db_session, admin, portfolio):  # noqa: F811,E501
    _without_plans(db_session, portfolio)

    def writer(messages, schema):
        """Never offers a plan - except in "no response", which keeps offering one."""
        asked = _asked(messages[1]["content"])
        texts = []
        for step_id, line in asked.items():
            text = good_text(line)
            text["body"] = text["body"].replace("You can pay in full or set up a plan on your", "You can pay on your")
            if step_id == "fu_no_activity":
                text["body"] = text["body"].replace("You can pay on your", "You can set up a payment plan on your")
            texts.append({"step_id": step_id, **text})
        return {"messages": texts}

    workflow = _suggest(db_session, admin, portfolio, writer)
    follow_ups = _follow_ups(workflow)
    assert list(follow_ups) == WITHOUT_PLAN_SETUP
    assert not follow_ups["fu_no_activity"]["config"].get("body")  # refused: this client has no payment plans
    assert all(follow_ups[step_id]["config"]["body"] for step_id in WITHOUT_PLAN_SETUP if step_id != "fu_no_activity")


# --------------------------------------------------------------------------- 3. their own translation


def test_follow_ups_that_cannot_be_translated_leave_the_language_to_the_rest(db_session, admin, portfolio):  # noqa: F811,E501
    _make_french(db_session, portfolio["portfolio_id"])
    translate, translations = fake_writer()

    def writer(messages, schema):
        prompt = messages[1]["content"]
        if TRANSLATE in prompt and '"step_id": "fu_' in prompt:
            raise ServiceUnavailableError("AI is busy")
        return translate(messages, schema)

    workflow = _suggest(db_session, admin, portfolio, writer)
    assert all(config["translations"]["FR"]["body"] for config in flowchart_messages(workflow).values())
    assert not any(step["config"].get("translations") for step in _follow_ups(workflow).values())
    assert context(workflow)["Languages"] == (
        "English (main), French; follow-ups not written in French - these customers get the planned messages instead"
    )


def test_the_languages_line_tells_the_follow_ups_apart():
    def message(step_id, *, follow_up=False, languages=()):
        config = {"body": "Hi", "translations": {code: {"body": "Salut"} for code in languages}}
        if follow_up:
            config["follow_up_for"] = "no_activity"
        return {"id": step_id, "kind": "Communication", "config": config}

    steps = [message("s1", languages=["FR"]), message("fu", follow_up=True)]
    assert languages_line_for_steps("English", ["FR"], steps)[0]["value"] == (
        "English (main), French; follow-ups not written in French - these customers get the planned messages instead"
    )
    steps[1] = message("fu", follow_up=True, languages=["FR"])
    assert languages_line_for_steps("English", ["FR"], steps)[0]["value"] == "English (main), French"


def test_a_customer_gets_no_follow_up_in_a_language_they_do_not_read():
    def in_french(step_ids):
        return [{**step, "config": {**step["config"], "translations": {"FR": {"body": "Salut"}}}}
                if step["id"] in step_ids else step for step in STEPS]

    planned_in_french = _context("opened_not_clicked", steps=in_french({"s2"}), language="FR")
    decision = decide_next_action(planned_in_french)
    assert decision["action"] == "keep_plan" and "not written in the customer's language" in decision["reason"]

    both = decide_next_action(_context("opened_not_clicked", steps=in_french({"s2", "f-opened"}), language="FR"))
    neither = decide_next_action(_context("opened_not_clicked", language="FR"))  # all of it in the main language
    english = decide_next_action(_context("opened_not_clicked", steps=in_french({"s2"}), language="EN"))
    assert [d["action"] for d in (both, neither, english)] == ["use_follow_up"] * 3
