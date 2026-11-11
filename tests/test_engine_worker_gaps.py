"""Worker fixes: Outcome "Paid In Full", Case / Payment Actions, wait timing, Customer Response.

- Outcome "Paid In Full" never clears a balance that is still owed.
- Case Actions and Payment Actions change the case, not only its timeline.
- A wait timed from a date long before the run started does not collapse the whole sequence.
- PayFlow's own messages are not a customer response.
"""

from datetime import date, datetime, timedelta, timezone

from app.infrastructure.database.models import (
    PayflowAccountModel,
    PayflowClientModel,
    PayflowHumanReviewModel,
    PayflowStrategyModel,
    PayflowWorkflowJobModel,
    PayflowWorkflowRunModel,
    PayflowWorkflowStepRunModel,
)
from app.modules.payflow.services.workflow_condition_runtime import (
    PAID_IN_FULL_NOT_PAID,
    apply_outcome_to_account,
    customer_response,
)
from app.modules.payflow.services.workflow_execution_service import WorkflowExecutionService
from app.shared.enums import PayflowCollectionStatus, PayflowWorkflowRunStatus

_SEQ = 0


def _setup(db, steps, *, outstanding=500.0, started_days_ago=0, **account_values):
    global _SEQ
    _SEQ += 1
    client = PayflowClientModel(code=f"gap-c-{_SEQ}", name=f"Gap Client {_SEQ}", governance_rules=[])
    db.add(client)
    db.flush()
    account = PayflowAccountModel(
        client_id=client.id,
        customer_name="Test User",
        account_reference=f"GAP-{_SEQ}",
        case_reference=f"CASE-GAP-{_SEQ}",
        original_balance=500.0,
        outstanding_balance=outstanding,
        recovered_balance=500.0 - outstanding,
        collection_status="Active",
        email="a@example.com",
        **account_values,
    )
    strategy = PayflowStrategyModel(
        code=f"gap-wf-{_SEQ}", name="Gap WF", client_id=client.id, status="Active",
        steps=steps, entry_node_id=steps[0]["id"],
    )
    db.add_all([account, strategy])
    db.flush()
    run = PayflowWorkflowRunModel(
        account_id=account.id,
        strategy_id=strategy.id,
        client_id=client.id,
        status=PayflowWorkflowRunStatus.ACTIVE.value,
        current_step_id=steps[0]["id"],
        strategy_version=1,
        started_at=datetime.now(timezone.utc) - timedelta(days=started_days_ago),
    )
    db.add(run)
    db.commit()
    return account, strategy, run


def _pending_step_ids(db, run):
    rows = (
        db.query(PayflowWorkflowJobModel)
        .filter(PayflowWorkflowJobModel.run_id == run.id, PayflowWorkflowJobModel.status == "pending")
        .all()
    )
    return [r.step_id for r in rows]


def _last_step_run(db, run, step_id):
    return (
        db.query(PayflowWorkflowStepRunModel)
        .filter_by(run_id=run.id, step_id=step_id)
        .order_by(PayflowWorkflowStepRunModel.id.desc())
        .first()
    )


def _case_action(action, *, next_id=None, title=None):
    return {"id": "s1", "kind": "Case Action", "title": title or action, "config": {"action": action}, "next": next_id}


# --------------------------------------------------------------------------- Outcome "Paid In Full"


def test_paid_in_full_outcome_never_clears_a_balance_that_is_still_owed(db_session):
    account, _, run = _setup(
        db_session, [{"id": "s1", "kind": "Outcome", "title": "Paid", "config": {"outcome": "Paid In Full"}}]
    )

    WorkflowExecutionService(db_session).execute_step(run.id, "s1")
    db_session.refresh(account)
    db_session.refresh(run)

    assert float(account.outstanding_balance) == 500.0 and float(account.recovered_balance) == 0.0
    assert account.collection_status == "Active"  # not Resolved
    assert account.human_review is True and account.next_action == PAID_IN_FULL_NOT_PAID
    assert run.status == PayflowWorkflowRunStatus.COMPLETED.value
    assert _last_step_run(db_session, run, "s1").result_json["applied"] is False


def test_paid_in_full_outcome_resolves_a_case_that_is_paid(db_session):
    account, _, run = _setup(
        db_session,
        [{"id": "s1", "kind": "Outcome", "title": "Paid", "config": {"outcome": "Paid In Full"}}],
        outstanding=0.0,
    )

    WorkflowExecutionService(db_session).execute_step(run.id, "s1")
    db_session.refresh(account)

    assert account.collection_status == PayflowCollectionStatus.RESOLVED.value
    assert float(account.recovered_balance) == 500.0  # unchanged: no payment is invented
    assert _last_step_run(db_session, run, "s1").result_json["applied"] is True


def test_case_closed_outcome_keeps_the_balance(db_session):
    account, _, _ = _setup(db_session, [{"id": "s1", "kind": "Outcome", "config": {"outcome": "Case Closed"}}])
    applied = apply_outcome_to_account(account, "Case Closed")
    assert applied["applied"] is True
    assert account.collection_status == PayflowCollectionStatus.RESOLVED.value
    assert float(account.outstanding_balance) == 500.0


# --------------------------------------------------------------------------- Case Actions


def test_close_case_resolves_the_case_and_its_closing_outcome_completes_the_run(db_session):
    steps = [
        _case_action("Close Case", next_id="s2"),
        {"id": "s2", "kind": "Outcome", "title": "Closed", "config": {"outcome": "Case Closed"}},
    ]
    account, _, run = _setup(db_session, steps)
    engine = WorkflowExecutionService(db_session)

    engine.execute_step(run.id, "s1")
    db_session.refresh(account)
    assert account.collection_status == PayflowCollectionStatus.RESOLVED.value
    assert _pending_step_ids(db_session, run) == ["s2"]

    engine.execute_step(run.id, "s2")  # a Resolved case blocks every step but its closing Outcome
    db_session.refresh(run)
    assert run.status == PayflowWorkflowRunStatus.COMPLETED.value


def test_a_message_after_close_case_is_not_sent(db_session):
    steps = [
        _case_action("Close Case", next_id="s2"),
        {"id": "s2", "kind": "Communication", "title": "Reminder", "config": {"channel": "Email"}},
    ]
    _, _, run = _setup(db_session, steps)
    engine = WorkflowExecutionService(db_session)

    engine.execute_step(run.id, "s1")
    engine.execute_step(run.id, "s2")
    db_session.refresh(run)

    assert run.status == PayflowWorkflowRunStatus.CANCELLED.value
    assert _last_step_run(db_session, run, "s2") is None  # never started


def test_hold_automated_contact_pauses_the_run_until_a_supervisor_releases_it(db_session):
    steps = [
        _case_action("Hold Automated Contact", next_id="s2", title="Hold contact during dispute"),
        {"id": "s2", "kind": "Case Action", "title": "After the hold", "config": {"action": "Flag"}},
    ]
    account, _, run = _setup(db_session, steps)
    engine = WorkflowExecutionService(db_session)

    engine.execute_step(run.id, "s1")
    db_session.refresh(account)
    db_session.refresh(run)

    assert run.status == PayflowWorkflowRunStatus.PAUSED_HUMAN_REVIEW.value
    assert account.collection_status == PayflowCollectionStatus.HUMAN_REVIEW.value  # no other workflow enrols it
    assert _pending_step_ids(db_session, run) == []  # nothing goes out while it is held
    review = db_session.get(PayflowHumanReviewModel, run.human_review_id)
    assert review.reason == "Customer dispute raised"  # read from the step's title
    assert "on hold" in review.proposed_action

    # The supervisor continues the workflow: the held step is not held again, the next step runs.
    engine.resume_run(run.id, from_step_id="s1", after_human_review=True)
    job = db_session.query(PayflowWorkflowJobModel).filter_by(run_id=run.id, status="pending").one()
    assert job.step_id == "s1" and (job.payload or {}).get("after_human_review") is True
    engine.execute_step(run.id, "s1", after_human_review=True)
    db_session.refresh(run)
    assert run.status == PayflowWorkflowRunStatus.ACTIVE.value
    assert "s2" in _pending_step_ids(db_session, run)


def test_escalation_followed_by_a_human_review_step_opens_one_review(db_session):
    steps = [
        _case_action("Move Case To Escalated Treatment", next_id="s2"),
        {"id": "s2", "kind": "Human Review", "title": "Supervisor review", "config": {"note": "decide"}},
    ]
    account, _, run = _setup(db_session, steps)
    engine = WorkflowExecutionService(db_session)

    engine.execute_step(run.id, "s1")
    db_session.refresh(account)
    db_session.refresh(run)
    assert account.collection_status == PayflowCollectionStatus.HUMAN_REVIEW.value
    assert run.status == PayflowWorkflowRunStatus.ACTIVE.value and run.human_review_id is None
    assert _pending_step_ids(db_session, run) == ["s2"]  # the Human Review step opens the review

    engine.execute_step(run.id, "s2")
    assert db_session.query(PayflowHumanReviewModel).filter_by(account_id=account.id).count() == 1


def test_escalation_as_the_last_step_opens_a_review(db_session):
    account, _, run = _setup(db_session, [_case_action("Move Case To Escalated Treatment")])

    WorkflowExecutionService(db_session).execute_step(run.id, "s1")
    db_session.refresh(run)

    assert run.status == PayflowWorkflowRunStatus.PAUSED_HUMAN_REVIEW.value
    assert db_session.query(PayflowHumanReviewModel).filter_by(account_id=account.id).count() == 1


def test_request_contact_details_update_flags_the_case_and_goes_on(db_session):
    steps = [
        _case_action("Request Contact Details Update", next_id="s2"),
        {"id": "s2", "kind": "Case Action", "title": "Next", "config": {"action": "Flag"}},
    ]
    account, _, run = _setup(db_session, steps)

    WorkflowExecutionService(db_session).execute_step(run.id, "s1")
    db_session.refresh(account)
    db_session.refresh(run)

    assert account.human_review is True
    assert account.collection_status == "Active"
    assert run.status == PayflowWorkflowRunStatus.ACTIVE.value and _pending_step_ids(db_session, run) == ["s2"]
    assert any(item.get("label") == "Request Contact Details Update" for item in account.timeline)


def test_request_contact_details_update_as_the_last_step_leaves_the_flag(db_session):
    account, _, run = _setup(db_session, [_case_action("Request Contact Details Update")])

    WorkflowExecutionService(db_session).execute_step(run.id, "s1")
    db_session.refresh(account)

    assert account.human_review is True


def test_an_unknown_case_action_still_only_records_itself(db_session):
    account, _, run = _setup(db_session, [_case_action("Flag")])

    WorkflowExecutionService(db_session).execute_step(run.id, "s1")
    db_session.refresh(account)

    assert account.collection_status == "Active" and not account.human_review


# --------------------------------------------------------------------------- Payment Actions


def test_record_promise_to_pay_makes_the_case_a_promise(db_session):
    steps = [{"id": "s1", "kind": "Payment Action", "title": "Record promise", "config": {"action": "Record Promise To Pay"}}]
    account, _, run = _setup(db_session, steps)

    WorkflowExecutionService(db_session).execute_step(run.id, "s1")
    db_session.refresh(account)

    assert account.collection_status == PayflowCollectionStatus.PROMISE_TO_PAY.value
    assert account.last_payment_amount is None  # no payment is recorded for a promise


def test_offer_payment_plan_does_not_change_the_case_status(db_session):
    steps = [{"id": "s1", "kind": "Payment Action", "title": "Offer plan", "config": {"action": "Offer Payment Plan"}}]
    account, _, run = _setup(db_session, steps)

    WorkflowExecutionService(db_session).execute_step(run.id, "s1")
    db_session.refresh(account)

    assert account.collection_status == "Active"


# --------------------------------------------------------------------------- wait timing


def _wait(reference_event, amount=7, direction="After"):
    return {"amount": amount, "unit": "Days", "direction": direction, "reference_event": reference_event}


def _sent_message(db, run, *, finished_at=None):
    db.add(
        PayflowWorkflowStepRunModel(
            run_id=run.id, step_id="msg", kind="Communication", attempt=1, status="done",
            finished_at=finished_at or datetime.now(timezone.utc),
        )
    )
    db.flush()


def test_first_message_on_an_old_account_is_still_sent_at_once(db_session):
    account, strategy, run = _setup(db_session, [_case_action("Flag")], date_listed=date.today() - timedelta(days=90))
    engine, cfg = WorkflowExecutionService(db_session), _wait("Case Received")

    when = engine._wait_until(cfg, {"config": cfg}, strategy, account=account, run=run)

    assert abs((when - datetime.now(timezone.utc)).total_seconds()) < 5  # overdue: now


def test_wait_from_an_old_case_received_date_keeps_the_spacing_after_a_message(db_session):
    """Before: "7 days after Case Received" on an old account was over already, so every message of
    the workflow followed the first one within seconds."""
    account, strategy, run = _setup(db_session, [_case_action("Flag")], date_listed=date.today() - timedelta(days=90))
    _sent_message(db_session, run)
    engine = WorkflowExecutionService(db_session)

    for reference in ("Case Received", "Due Date"):
        account.due_date = date.today() - timedelta(days=60)
        cfg = _wait(reference)
        when = engine._wait_until(cfg, {"config": cfg}, strategy, account=account, run=run)
        expected = datetime.now(timezone.utc) + timedelta(days=7)  # 7 days from the run's start
        assert abs((when - expected).total_seconds()) < 5, reference


def test_a_date_after_the_run_started_is_used_as_it_is(db_session):
    account, strategy, run = _setup(db_session, [_case_action("Flag")], due_date=date.today() + timedelta(days=2))
    _sent_message(db_session, run)
    cfg = _wait("Due Date", amount=3)

    when = WorkflowExecutionService(db_session)._wait_until(cfg, {"config": cfg}, strategy, account=account, run=run)

    due = datetime.combine(account.due_date, datetime.min.time(), tzinfo=timezone.utc)
    assert when == due + timedelta(days=3)


def test_before_a_date_is_not_moved(db_session):
    """ "3 days before Due Date" with the due date long past stays overdue (sent at once)."""
    account, strategy, run = _setup(db_session, [_case_action("Flag")], due_date=date.today() - timedelta(days=60))
    _sent_message(db_session, run)
    cfg = _wait("Due Date", amount=3, direction="Before")

    when = WorkflowExecutionService(db_session)._wait_until(cfg, {"config": cfg}, strategy, account=account, run=run)

    assert abs((when - datetime.now(timezone.utc)).total_seconds()) < 5


def test_previous_action_is_the_runs_previous_step_to_the_second(db_session):
    """Before: the account's last contact *date* (midnight), so a 2-day wait ended up to a day early."""
    account, strategy, run = _setup(db_session, [_case_action("Flag")], last_contact_date=date.today())
    sent_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    _sent_message(db_session, run, finished_at=sent_at)
    cfg = _wait("Previous Action", amount=2)

    when = WorkflowExecutionService(db_session)._wait_until(cfg, {"config": cfg}, strategy, account=account, run=run)

    assert abs((when - (sent_at + timedelta(days=2))).total_seconds()) < 5


def test_previous_action_without_an_earlier_step_falls_back_to_the_contact_dates(db_session):
    account, strategy, run = _setup(
        db_session, [_case_action("Flag")], last_contact_date=date.today() + timedelta(days=1)
    )
    cfg = _wait("Previous Action", amount=2)

    when = WorkflowExecutionService(db_session)._wait_until(cfg, {"config": cfg}, strategy, account=account, run=run)

    contact = datetime.combine(account.last_contact_date, datetime.min.time(), tzinfo=timezone.utc)
    assert when == contact + timedelta(days=2)


# --------------------------------------------------------------------------- Customer Response


def test_our_own_message_is_not_a_customer_response(db_session):
    account, _, _ = _setup(db_session, [_case_action("Flag")], last_contact_date=date.today())
    assert customer_response(db_session, account, {}) == "None"  # contacted, but no answer


def test_a_real_reply_or_dispute_is_still_found(db_session):
    account, _, _ = _setup(db_session, [_case_action("Flag")], last_contact_date=date.today())
    assert customer_response(db_session, account, {"customer_response": "reply"}) == "Received"

    account.timeline = [{"label": "Inbound SMS", "detail": "customer reply"}]
    assert customer_response(db_session, account, {}) == "Received"

    account.timeline = [{"label": "Dispute", "detail": "customer says the amount is wrong"}]
    assert customer_response(db_session, account, {}) == "Dispute Raised"
