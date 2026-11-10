"""Human review decisions must leave the workflow run in a sensible state."""

from datetime import date, timedelta

from app.infrastructure.database.models import (
    PayflowAccountModel,
    PayflowClientModel,
    PayflowHumanReviewModel,
    PayflowStrategyModel,
    PayflowWorkflowJobModel,
    PayflowWorkflowRunModel,
    UserModel,
)
from app.modules.payflow.services.review_service import PayflowReviewService
from app.modules.payflow.services.workflow_execution_service import WorkflowExecutionService
from app.shared.enums import (
    PayflowReviewStatus,
    PayflowWorkflowJobType,
    PayflowWorkflowRunStatus,
)

STEPS = [
    {
        "id": "s1",
        "kind": "Human Review",
        "title": "Supervisor review",
        "config": {"review_reason": "Payment arrangement exception"},
        "next": "s2",
    },
    {"id": "s2", "kind": "Outcome", "title": "Done", "config": {"outcome": "Done"}},
]


def _paused_run(db):
    """An account enrolled on a workflow and paused at its Human Review step."""
    client = PayflowClientModel(code="hr-c", name="HR Client", governance_rules=[])
    db.add(client)
    db.flush()
    account = PayflowAccountModel(
        client_id=client.id,
        customer_name="Test User",
        account_reference="HR-1",
        case_reference="CASE-HR-1",
        original_balance=700.0,
        outstanding_balance=700.0,
        collection_status="Active",
    )
    strategy = PayflowStrategyModel(
        code="hr-wf", name="HR WF", client_id=client.id, status="Active",
        steps=STEPS, entry_node_id="s1",
    )
    db.add_all([account, strategy])
    db.commit()
    svc = WorkflowExecutionService(db)
    svc.enroll_strategy(strategy.id)
    run = db.query(PayflowWorkflowRunModel).filter_by(account_id=account.id).one()
    for job in db.query(PayflowWorkflowJobModel).filter_by(run_id=run.id).all():
        job.status = "done"
    db.commit()
    svc.execute_step(run.id, "s1")
    db.refresh(run)
    assert run.status == PayflowWorkflowRunStatus.PAUSED_HUMAN_REVIEW.value
    review = db.get(PayflowHumanReviewModel, run.human_review_id)
    return account, strategy, run, review


def _admin(db):
    return db.query(UserModel).filter(UserModel.email == "admin@payflow.ai").one()


def _pending(db, run):
    return (
        db.query(PayflowWorkflowJobModel)
        .filter(PayflowWorkflowJobModel.run_id == run.id, PayflowWorkflowJobModel.status == "pending")
        .all()
    )


def test_rejected_review_does_not_re_enroll_account_on_same_workflow(db_session):
    account, strategy, run, review = _paused_run(db_session)

    PayflowReviewService(db_session).reject(
        _admin(db_session), review.id, reason="Incorrect treatment"
    )
    db_session.refresh(run)
    assert run.status == PayflowWorkflowRunStatus.CANCELLED.value

    # The next daily import queues enroll again for every Active workflow.
    result = WorkflowExecutionService(db_session).enroll_strategy(strategy.id)

    assert result["enrolled"] == 0
    assert db_session.query(PayflowWorkflowRunModel).filter_by(account_id=account.id).count() == 1
    assert db_session.query(PayflowHumanReviewModel).filter_by(account_id=account.id).count() == 1


def test_modify_to_hold_stops_the_workflow_instead_of_continuing(db_session):
    _account, _strategy, run, review = _paused_run(db_session)

    PayflowReviewService(db_session).modify(
        _admin(db_session),
        review.id,
        action="Hold collection activity pending dispute review",
    )

    db_session.refresh(run)
    assert run.status == PayflowWorkflowRunStatus.CANCELLED.value
    assert _pending(db_session, run) == []


def test_modify_to_continue_resumes_the_workflow(db_session):
    _account, _strategy, run, review = _paused_run(db_session)

    PayflowReviewService(db_session).modify(
        _admin(db_session),
        review.id,
        action="Continue current treatment with adjusted communication",
    )

    jobs = _pending(db_session, run)
    assert [j.job_type for j in jobs] == [PayflowWorkflowJobType.RESUME_RUN.value]


def test_review_held_until_a_date_is_released_when_the_date_arrives(db_session):
    _account, _strategy, _run, review = _paused_run(db_session)
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    svc = PayflowReviewService(db_session)
    svc.hold(_admin(db_session), review.id, until=yesterday)

    released = svc.release_due_holds()

    db_session.refresh(review)
    assert released == 1
    assert review.status == PayflowReviewStatus.AWAITING_REVIEW.value
    assert review.hold_until is None


def test_review_held_until_a_future_date_stays_on_hold(db_session):
    _account, _strategy, _run, review = _paused_run(db_session)
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    svc = PayflowReviewService(db_session)
    svc.hold(_admin(db_session), review.id, until=tomorrow)

    assert svc.release_due_holds() == 0
    db_session.refresh(review)
    assert review.status == PayflowReviewStatus.ON_HOLD.value


def test_review_held_without_a_date_is_never_released_automatically(db_session):
    _account, _strategy, _run, review = _paused_run(db_session)
    svc = PayflowReviewService(db_session)
    svc.hold(_admin(db_session), review.id, until="Further review")

    assert svc.release_due_holds() == 0
    db_session.refresh(review)
    assert review.status == PayflowReviewStatus.ON_HOLD.value


def test_deactivating_workflow_closes_its_open_review_and_frees_the_account(db_session):
    account, strategy, _run, review = _paused_run(db_session)

    WorkflowExecutionService(db_session).on_strategy_deactivated(strategy.id)

    db_session.refresh(review)
    db_session.refresh(account)
    assert review.status == PayflowReviewStatus.REJECTED.value
    assert account.human_review is False
    assert account.collection_status == "Active"


def test_hold_arriving_during_review_keeps_run_paused_so_approval_still_resumes(db_session):
    account, _strategy, run, review = _paused_run(db_session)
    svc = WorkflowExecutionService(db_session)

    # Daily import puts the case on provincial hold while the review is open.
    svc.on_case_status_changed(
        account.id, previous="Human Review", new="Human Review", provincial_hold=True
    )
    db_session.refresh(run)
    assert run.status == PayflowWorkflowRunStatus.PAUSED_HUMAN_REVIEW.value

    PayflowReviewService(db_session).approve(_admin(db_session), review.id)

    assert [j.job_type for j in _pending(db_session, run)] == [
        PayflowWorkflowJobType.RESUME_RUN.value
    ]
