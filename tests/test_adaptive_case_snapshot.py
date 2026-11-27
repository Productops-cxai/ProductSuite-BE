"""Adaptive workflows: waiting decisions and the case page's data (plan segment 6).

1. Only the latest AI decision waits for approval: a newer one replaces it (as does any change carried out).
2. A waiting decision that no longer applies - the case ended, the message it would replace went out, nothing
   is left to move - is closed as "expired" by the worker's sweep, with the reason.
3. The run snapshot (GET /workflow-runs/{id}/adaptive) shows, on a follow-up decision, the text this customer
   would get (their audience version, in their language), and the messages sent - which text went out and,
   for a follow-up, the planned message it replaced.
"""

from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

from app.infrastructure.database.models import (
    PayflowAccountModel,
    PayflowAdaptiveDecisionModel,
    PayflowCommunicationModel,
)
from app.modules.payflow.services import adaptive_workflow_service
from app.modules.payflow.services.adaptive_workflow_service import AdaptiveWorkflowService
from tests.test_adaptive_next_action import _message, _send
from tests.test_adaptive_rule_fixes import WORKER_STEPS, _decisions, _event, _planned, _run


def _decide(**decision):
    answer = {"decided_by": "ai", "reason": f"AI: {decision['action']}", "confidence": 80, **decision}
    return patch.object(adaptive_workflow_service, "decide_next_action", return_value=answer)


def _waiting(db, run, action, **fields):
    row = PayflowAdaptiveDecisionModel(run_id=run.id, account_id=run.account_id, strategy_id=run.strategy_id,
                                       action=action, decided_by="ai", status="awaiting_approval", **fields)
    db.add(row)
    db.commit()
    return row


# --------------------------------------------------------------------------- 1. only the latest waits


def test_a_newer_waiting_decision_replaces_the_older_one(db_session):
    run = _run(db_session, WORKER_STEPS)  # a supervised client: AI changes wait for approval
    _planned(db_session, run, "s2", hours=48)
    service = AdaptiveWorkflowService(db_session)
    with _decide(action="switch_channel", channel="SMS"):
        service.reassess(run.id, trigger="event")
    with _decide(action="move_next", at=(datetime.now(timezone.utc) + timedelta(days=3)).isoformat()):
        service.reassess(run.id, trigger="event")

    db_session.refresh(run)
    assert _decisions(db_session, run) == [("switch_channel", "superseded"), ("move_next", "awaiting_approval")]
    assert run.preferred_channel is None  # neither was carried out


def test_a_change_carried_out_replaces_a_waiting_decision(db_session):
    run = _run(db_session, WORKER_STEPS)
    _planned(db_session, run, "s2", hours=48)
    service = AdaptiveWorkflowService(db_session)
    with _decide(action="switch_channel", channel="SMS"):
        service.reassess(run.id, trigger="event")
    with _decide(action="use_follow_up", decided_by="rules", replace_step_id="s2", follow_up_step_id="f-failed"):
        service.reassess(run.id, trigger="event")

    assert _decisions(db_session, run) == [("switch_channel", "superseded"), ("use_follow_up", "executed")]


# --------------------------------------------------------------------------- 2. expired by the sweep


def test_waiting_decisions_that_no_longer_apply_expire(db_session):
    ended = _run(db_session, WORKER_STEPS)
    ended.status = "Completed"
    sent = _run(db_session, WORKER_STEPS)
    db_session.add(PayflowCommunicationModel(code="COM-SNAP-1", client_id=sent.client_id, account_id=sent.account_id,
                                             run_id=sent.id, step_id="s2", channel="Email", purpose="Payment Reminder",
                                             status="Sent", sent_at=datetime.now(timezone.utc)))
    nothing_planned = _run(db_session, WORKER_STEPS)
    still_planned = _run(db_session, WORKER_STEPS)
    _planned(db_session, still_planned, "s2", hours=48)
    later = datetime.now(timezone.utc) + timedelta(days=2)
    rows = [
        _waiting(db_session, ended, "switch_channel", channel="SMS"),
        _waiting(db_session, sent, "use_follow_up", replace_step_id="s2", follow_up_step_id="f-failed"),
        _waiting(db_session, nothing_planned, "move_next", at=later),
        _waiting(db_session, still_planned, "move_next", at=later),
    ]

    assert AdaptiveWorkflowService(db_session).expire_waiting_decisions() == 3
    db_session.commit()
    for row in rows:
        db_session.refresh(row)
    assert [(row.status, (row.outcome or {}).get("expired_reason")) for row in rows] == [
        ("expired", "run_terminal"),
        ("expired", "step_already_sent"),
        ("expired", "no_pending_step"),
        ("awaiting_approval", None),
    ]


# --------------------------------------------------------------------------- 3. the case page's data


def _with_versions(step):
    """The follow-up written for everyone and for 65+, each also in French."""
    config = {
        **step["config"],
        "subject": "Main subject", "body": "Main body", "sms_body": "Main SMS",
        "translations": {"FR": {"subject": "Sujet", "body": "Texte", "sms_body": "SMS"}},
        "versions": {"age_65_plus": {"label": "Age 65 and over", "subject": "65 subject", "body": "65 body",
                                     "sms_body": "65 SMS",
                                     "translations": {"FR": {"subject": "Sujet 65", "body": "Texte 65",
                                                             "sms_body": "SMS 65"}}}},
    }
    return {**step, "config": config}


PREVIEW_STEPS = [*WORKER_STEPS[:3], _with_versions(_message("f-failed", "Payment failed", follow_up_for="payment_failed"))]


def _preview(db, *, born=None, language=None):
    run = _run(db, PREVIEW_STEPS)
    account = db.get(PayflowAccountModel, run.account_id)
    account.date_of_birth, account.language = born, language
    _waiting(db, run, "use_follow_up", replace_step_id="s2", follow_up_step_id="f-failed")
    return AdaptiveWorkflowService(db).get_run_adaptive(run.id)["decisions"][0]["preview"]


def test_a_follow_up_decision_shows_the_text_this_customer_would_get(db_session):
    senior = _preview(db_session, born=date(1950, 1, 1), language="FR")
    assert (senior["version"], senior["version_label"], senior["language"]) == ("age_65_plus", "Age 65 and over", "FR")
    assert (senior["subject"], senior["body"], senior["sms_body"]) == ("Sujet 65", "Texte 65", "SMS 65")
    assert (senior["step_id"], senior["title"], senior["level"]) == ("f-failed", "Payment failed", 1)

    everyone = _preview(db_session, language="EN")  # no age known, English (the main language)
    assert (everyone["version"], everyone["version_label"], everyone["language"]) == ("main", "Main text", None)
    assert everyone["body"] == "Main body"


def test_the_snapshot_lists_what_was_sent_and_what_a_follow_up_replaced(db_session):
    run = _run(db_session, WORKER_STEPS, current="s2")
    _event(db_session, run, "email_not_opened")
    _send(db_session, run, "s2")  # Email 2's slot: the "not opened" follow-up goes instead

    messages = AdaptiveWorkflowService(db_session).get_run_adaptive(run.id)["messages"]
    assert len(messages) == 1
    sent = messages[0]
    assert (sent["step_id"], sent["title"], sent["follow_up"], sent["follow_up_level"]) == (
        "f-unopened", "Not opened", True, 1)
    assert (sent["replaced_step_id"], sent["replaced_title"]) == ("s2", "Email 2")
    assert (sent["version"], sent["version_label"], sent["channel"]) == ("main", "Main text", "SMS")

    planned = _run(db_session, WORKER_STEPS, current="s2")
    _send(db_session, planned, "s2")
    plain = AdaptiveWorkflowService(db_session).get_run_adaptive(planned.id)["messages"][0]
    assert (plain["step_id"], plain["follow_up"], plain["replaced_step_id"]) == ("s2", False, None)


def test_when_a_check_comes_next_the_snapshot_still_names_the_next_message(db_session):
    steps = [
        _message("s1", "Email 1", channel="Email", next="c1"),
        {"id": "c1", "kind": "Condition", "title": "Still unpaid?",
         "config": {"attribute": "Payment Status", "operator": "Equals", "value": "Unpaid"}, "yes": "s2", "no": "s3"},
        *WORKER_STEPS[1:],
    ]
    run = _run(db_session, steps)
    _planned(db_session, run, "c1", hours=24)

    snapshot = AdaptiveWorkflowService(db_session).get_run_adaptive(run.id)
    assert (snapshot["next_scheduled"]["kind"], snapshot["next_scheduled"]["title"]) == ("Condition", "Still unpaid?")
    assert (snapshot["next_message"]["step_id"], snapshot["next_message"]["title"]) == ("s2", "Email 2")


def test_the_api_returns_the_preview_and_the_messages(client, admin_token, db_session):
    run = _run(db_session, PREVIEW_STEPS)
    _waiting(db_session, run, "use_follow_up", replace_step_id="s2", follow_up_step_id="f-failed")

    response = client.get(f"/payflow/workflow-runs/{run.id}/adaptive", headers={"Authorization": f"Bearer {admin_token}"})
    body = response.json()
    assert response.status_code == 200, body
    decision = body["decisions"][0]
    assert decision["preview"]["body"] == "Main body"
    assert (decision["replace_title"], decision["follow_up_title"]) == ("Email 2", "Payment failed")
    assert body["messages"] == [] and body["lined_up"] == []
    assert [(row["step_id"], row["state"]) for row in body["follow_ups"]] == [("f-failed", "available")]
