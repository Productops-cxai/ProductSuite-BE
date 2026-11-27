"""Adaptive workflows end to end (plan segment 9): one customer's journey through the real worker.

Every step and every reassessment runs as a worker job (app.worker.handlers.process_job), as in production:
  Email 1 → not opened: "not opened" follow-up lined up (by SMS) → opened late: taken back → Reminder 2 as planned
  → opened, not paid: first-time follow-up sent in Reminder 3's place, in the customer's 65+ version
  → the same again: its repeat lined up for Reminder 4 → payment failed: "payment failed" takes that place
  → silence the rules cannot answer: the AI suggests SMS; a supervised client, so it waits for approval while the
  plan keeps running → Reminder 4's slot sends "payment failed" → Operations approve: SMS from now on.
Also: a decision's notification is cleared once it no longer waits, and the follow-up types API.
"""

from datetime import date, datetime, timezone
from unittest.mock import patch

import pytest
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.infrastructure.database.models import (
    PayflowAccountModel,
    PayflowAdaptiveDecisionModel,
    PayflowClientModel,
    PayflowNotificationModel,
    PayflowStrategyModel,
    PayflowWorkflowJobModel,
    PayflowWorkflowRunModel,
)
from app.modules.payflow.services.adaptive_workflow_service import AdaptiveWorkflowService
from app.modules.payflow.services.engagement_service import record_engagement_event
from app.modules.payflow.services.workflow_execution_service import WorkflowExecutionService
from app.modules.payflow.services.workflow_queue import enqueue_execute_step
from app.modules.payflow.services.workflow_suggestion import next_action_ai
from app.worker import handlers
from tests.test_adaptive_next_action import _message
from tests.test_workflow_regenerate import admin  # noqa: F401 (fixture)


@pytest.fixture()
def worker_db(db_session):
    """process_job opens its own session: point it at the test database."""
    factory = sessionmaker(autocommit=False, autoflush=False, bind=db_session.get_bind())
    with patch.object(handlers, "SessionLocal", factory), \
            patch.object(WorkflowExecutionService, "_payment_link", return_value=""):
        yield db_session


def _wait(step_id, next_id):
    return {"id": step_id, "kind": "Wait", "title": "Wait 2 days", "next": next_id,
            "config": {"amount": 2, "unit": "Days", "direction": "After", "reference_event": "Previous Action"}}


def _repeat(step):
    return {**step, "id": f"{step['id']}-2", "title": f"{step['title']} (repeat)",
            "config": {**step["config"], "follow_up_level": 2}}


OPENED = _message("f-opened", "Opened, not clicked", follow_up_for="opened_not_clicked")
OPENED["config"]["versions"] = {"age_65_plus": {"label": "Age 65 and over", "subject": "For you",
                                                "body": "65+ text {{payment_link}}"}}
STEPS = [
    _message("s1", "Email 1", next="w1"), _wait("w1", "s2"),
    _message("s2", "Reminder 2", next="w2"), _wait("w2", "s3"),
    _message("s3", "Reminder 3", next="w3"), _wait("w3", "s4"),
    _message("s4", "Reminder 4", next="o1"),
    {"id": "o1", "kind": "Outcome", "title": "Done", "config": {"outcome": "Done"}},
    _message("f-unopened", "Not opened", follow_up_for="email_not_opened"),
    OPENED, _repeat(OPENED),
    _message("f-failed", "Payment failed", follow_up_for="payment_failed"),
]

_SEQ = 0


def _journey(db):
    global _SEQ
    _SEQ += 1
    client = PayflowClientModel(code=f"e2e-{_SEQ}", name=f"E2E {_SEQ}", ai_mode="supervised_ai")
    db.add(client)
    db.flush()
    account = PayflowAccountModel(
        client_id=client.id, customer_name="Jane Doe", customer_first_name="Jane", account_reference=f"E2E-{_SEQ}",
        case_reference=f"CASE-E2E-{_SEQ}", original_balance=800.0, outstanding_balance=800.0,
        collection_status="Active", email="jane@example.com", phone_mobile="+15551234567",
        date_of_birth=date(1950, 1, 1),
    )
    strategy = PayflowStrategyModel(code=f"e2e-wf-{_SEQ}", name="E2E WF", client_id=client.id, status="Active",
                                    steps=STEPS, entry_node_id="s1")
    db.add_all([account, strategy])
    db.flush()
    run = PayflowWorkflowRunModel(account_id=account.id, strategy_id=strategy.id, client_id=client.id,
                                  status="Active", current_step_id="s1", strategy_version=1)
    db.add(run)
    db.flush()
    enqueue_execute_step(db, run_id=run.id, step_id="s1", account_id=account.id, strategy_id=strategy.id)
    db.commit()
    return run


def _work(db, run, job_type="execute_step"):
    """The worker claims the run's next waiting job of that type and runs it (its time is not waited for)."""
    job = (
        db.query(PayflowWorkflowJobModel)
        .filter_by(run_id=run.id, job_type=job_type, status="pending")
        .order_by(PayflowWorkflowJobModel.scheduled_at, PayflowWorkflowJobModel.id)
        .first()
    )
    assert job is not None, f"no {job_type} job waiting"
    job.status, job.locked_by, job.locked_at, job.attempts = "running", "e2e", datetime.now(timezone.utc), 1
    db.commit()
    handlers.process_job(job.id, worker_id="e2e")
    db.expire_all()
    return job


def _until_message(db, run, step_id):
    """Run the worker until the next waiting job is `step_id` (the waits in between included)."""
    for _ in range(6):
        pending = db.query(PayflowWorkflowJobModel).filter_by(run_id=run.id, job_type="execute_step",
                                                              status="pending").first()
        if pending and pending.step_id == step_id:
            return
        _work(db, run)
    raise AssertionError(f"{step_id} never came up")


def _event(db, run, kind):
    global _SEQ
    _SEQ += 1
    record_engagement_event(db, account_id=run.account_id, event_type=kind, source="test",
                            dedupe_key=f"e2e:{run.id}:{kind}:{_SEQ}", run_id=run.id)
    db.commit()
    _work(db, run, "reassess_run")
    db.refresh(run)


def _latest(db, run):
    return db.query(PayflowAdaptiveDecisionModel).filter_by(run_id=run.id).order_by(
        PayflowAdaptiveDecisionModel.id.desc()).first()


class FakeAI:
    def __call__(self, messages, schema, *, quick=False):
        return {"action": "switch_channel", "channel": "SMS", "confidence": 80,
                "reason": "Several emails went unanswered; SMS may reach her"}


def test_one_customers_journey_through_the_worker(worker_db, admin):  # noqa: F811
    db = worker_db
    run = _journey(db)
    _work(db, run)  # Email 1
    _until_message(db, run, "s2")

    # Not opened → its follow-up in Reminder 2's place, by SMS - a fixed rule: no approval, supervised or not.
    _event(db, run, "email_not_opened")
    decision = _latest(db, run)
    assert (decision.action, decision.decided_by, decision.status) == ("use_follow_up", "rules", "executed")
    assert (run.swaps, run.preferred_channel) == ({"s2": "f-unopened"}, "SMS")

    # Opened late → the follow-up no longer fits: taken back, and the SMS switch with it.
    _event(db, run, "email_opened")
    assert (_latest(db, run).action, run.swaps, run.preferred_channel) == ("drop_follow_up", None, None)
    _work(db, run)  # Reminder 2, as planned
    _until_message(db, run, "s3")

    # Opened, not paid → first-time follow-up in Reminder 3's place, in her 65+ version.
    _event(db, run, "opened_not_clicked")
    assert run.swaps == {"s3": "f-opened"}
    _work(db, run)
    _until_message(db, run, "s4")

    # The same again → the repeat is lined up for Reminder 4; then the payment fails → "payment failed" instead.
    _event(db, run, "opened_not_clicked")
    assert run.swaps == {"s4": "f-opened-2"}
    _event(db, run, "payment_failed")
    assert run.swaps == {"s4": "f-failed"}
    lined_up = AdaptiveWorkflowService(db).get_run_adaptive(run.id)["lined_up"]
    assert [(item["follow_up_title"], item["replace_title"]) for item in lined_up] == [("Payment failed", "Reminder 4")]

    # Silence with no follow-up left for it → the AI; a supervised client, so it waits - and Ops are notified.
    with patch.object(next_action_ai, "call_llm", FakeAI()):
        _event(db, run, "no_activity")
    waiting = _latest(db, run)
    assert (waiting.action, waiting.decided_by, waiting.status) == ("switch_channel", "ai", "awaiting_approval")
    bell = db.query(PayflowNotificationModel).filter_by(user_id=admin.id, entity_type="adaptive_decision",
                                                        entity_id=str(waiting.id)).one()
    assert bell.read_at is None and run.preferred_channel is None  # not carried out yet

    # The plan keeps running: Reminder 4's slot sends the "payment failed" follow-up.
    _work(db, run)
    assert run.status in {"Active", "Completed"}

    # Operations approve → SMS from now on; the notification is cleared.
    AdaptiveWorkflowService(db).approve_decision(waiting.id, user=admin, note="OK")
    db.expire_all()
    assert db.get(PayflowWorkflowRunModel, run.id).preferred_channel == "SMS"
    assert db.get(PayflowNotificationModel, bell.id).read_at is not None

    # What the case page shows.
    snapshot = AdaptiveWorkflowService(db).get_run_adaptive(run.id)
    sent = [(m["step_id"], m["version"], m["replaced_title"]) for m in snapshot["messages"]]
    assert sent == [
        ("f-failed", "main", "Reminder 4"),
        ("f-opened", "age_65_plus", "Reminder 3"),
        ("s2", "main", None),
        ("s1", "main", None),
    ]
    statuses = [(d["action"], d["status"]) for d in reversed(snapshot["decisions"])]
    assert statuses == [
        ("use_follow_up", "superseded"),  # "not opened" - taken back after the late open
        ("drop_follow_up", "executed"),
        ("use_follow_up", "executed"),  # "opened, not paid", sent in Reminder 3's place
        ("use_follow_up", "superseded"),  # its repeat - replaced by "payment failed"
        ("use_follow_up", "executed"),  # "payment failed", sent in Reminder 4's place
        ("switch_channel", "approved"),
    ]
    usage = {item["step_id"]: (item["state"], item["replaced_title"]) for item in snapshot["follow_ups"]}
    assert usage == {
        "f-unopened": ("available", None),  # lined up, then taken back: never sent
        "f-opened": ("sent", "Reminder 3"),
        "f-opened-2": ("available", None),  # replaced before it went out
        "f-failed": ("sent", "Reminder 4"),
    }
    assert snapshot["lined_up"] == []


# --------------------------------------------------------------------------- the bell


def _waiting(db, run, action="switch_channel"):
    row = PayflowAdaptiveDecisionModel(run_id=run.id, account_id=run.account_id, strategy_id=run.strategy_id,
                                       action=action, channel="SMS", decided_by="ai", status="awaiting_approval")
    db.add(row)
    db.flush()
    return row


def _bell(db, user, decision):
    note = PayflowNotificationModel(user_id=user.id, notification_type="adaptive_decision_awaiting", title="x",
                                    entity_type="adaptive_decision", entity_id=str(decision.id))
    db.add(note)
    db.commit()
    return note


def test_a_decision_that_no_longer_waits_leaves_the_bell(worker_db, admin):  # noqa: F811
    db = worker_db
    service = AdaptiveWorkflowService(db)
    rejected_run, expired_run, replaced_run, waiting_run = (_journey(db) for _ in range(4))
    rejected = _waiting(db, rejected_run)
    expired = _waiting(db, expired_run)
    expired_run.status = "Completed"  # the case ended
    replaced = _waiting(db, replaced_run)
    still = _waiting(db, waiting_run)
    bells = {name: _bell(db, admin, row) for name, row in
             (("rejected", rejected), ("expired", expired), ("replaced", replaced), ("still", still))}

    service.reject_decision(rejected.id, user=admin)
    service.expire_waiting_decisions()
    service._supersede_waiting(replaced_run, except_id=_waiting(db, replaced_run).id)
    db.commit()
    db.expire_all()

    read = {name: db.get(PayflowNotificationModel, bell.id).read_at is not None for name, bell in bells.items()}
    assert read == {"rejected": True, "expired": True, "replaced": True, "still": False}


def test_an_approved_change_that_cannot_be_carried_out_is_failed_not_approved(worker_db, admin):  # noqa: F811
    db = worker_db
    run = _journey(db)
    db.get(PayflowAccountModel, run.account_id).phone_mobile = None  # SMS cannot reach her
    decision = _waiting(db, run)
    db.commit()

    result = AdaptiveWorkflowService(db).approve_decision(decision.id, user=admin)
    assert result["status"] == "failed" and "not allowed" in result["outcome"]["error"]
    assert db.get(PayflowWorkflowRunModel, run.id).preferred_channel is None


# --------------------------------------------------------------------------- the follow-up types


def test_the_follow_up_types_api_gives_the_screen_its_names_and_times(client, admin_token):
    with patch.object(settings, "ADAPTIVE_NO_ACTIVITY_HOURS", 96):
        response = client.get("/payflow/adaptive/follow-up-types", headers={"Authorization": f"Bearer {admin_token}"})
    assert response.status_code == 200, response.json()
    types = {item["follow_up_for"]: item for item in response.json()["items"]}
    assert len(types) == 7
    quiet = types["no_activity"]
    assert (quiet["label"], quiet["waiting_hours"]) == ("No Engagement", 96)
    assert "96 hours" in quiet["trigger"]
    assert [(level["level"], level["name"]) for level in quiet["levels"]] == [(1, "First time"), (2, "Repeat")]
    plan = types["plan_setup_not_finished"]
    assert (plan["waiting_hours"], plan["needs_payment_plans"]) == (24, True)
    assert types["payment_failed"]["waiting_hours"] == 2
