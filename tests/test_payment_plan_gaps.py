"""Payment plans can be stopped, missed instalments are followed up, money is not miscounted."""

from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app.core.exceptions import ValidationAppError
from app.infrastructure.database.models import (
    PayflowAccountModel,
    PayflowClientModel,
    PayflowOperationalExceptionModel,
    PayflowPaymentAttemptModel,
    PayflowPaymentLinkModel,
    PayflowPaymentPlanInstallmentModel,
    PayflowPaymentPlanModel,
    PayflowPaymentTransactionModel,
    UserModel,
)
from app.modules.payflow.services import plan_auto_charge_service
from app.modules.payflow.services.payment_outcome_service import PayflowPaymentOutcomeService
from app.modules.payflow.services.payment_portal_service import PayflowPaymentPortalService
from app.modules.payflow.services.plan_auto_charge_service import PayflowPlanAutoChargeService
from app.modules.payflow.services.workflow_execution_service import WorkflowExecutionService

_SEQ = 0
NOW = datetime.now(timezone.utc)


def _n():
    global _SEQ
    _SEQ += 1
    return _SEQ


def _account(db, *, outstanding=1000.0, status="Active"):
    n = _n()
    client = PayflowClientModel(code=f"pay-c-{n}", name=f"Pay Client {n}")
    db.add(client)
    db.flush()
    account = PayflowAccountModel(
        client_id=client.id,
        customer_name="Test User",
        account_reference=f"PAY-{n}",
        case_reference=f"CASE-PAY-{n}",
        original_balance=1000.0,
        outstanding_balance=outstanding,
        recovered_balance=1000.0 - outstanding,
        collection_status=status,
        currency_code="CAD",
    )
    db.add(account)
    db.flush()
    link = PayflowPaymentLinkModel(
        token=f"tok-{n}",
        client_id=client.id,
        account_id=account.id,
        expires_at=NOW + timedelta(days=7),
    )
    db.add(link)
    db.commit()
    return account, link


def _attempt(db, account, link, amount, *, option="partial", status="Processing", **extra):
    attempt = PayflowPaymentAttemptModel(
        public_id=f"att-{_n()}",
        payment_link_id=link.id,
        client_id=account.client_id,
        account_id=account.id,
        option_type=option,
        amount=amount,
        currency_code="CAD",
        status=status,
        provider="mock",
        balance_at_initiation=account.outstanding_balance,
        **extra,
    )
    db.add(attempt)
    db.commit()
    return attempt


def _plan(db, account, link, *, installments=("Pending", "Pending"), due=None, **extra):
    plan = PayflowPaymentPlanModel(
        public_id=f"plan-{_n()}",
        client_id=account.client_id,
        account_id=account.id,
        payment_link_id=link.id,
        status="Active",
        installments_total=len(installments),
        installment_amount=250.0,
        total_payable=250.0 * len(installments),
        start_date=date.today(),
        currency_code="CAD",
        auto_charge_enabled=True,
        stripe_customer_id="cus_test",
        stripe_payment_method_id="pm_test",
        **extra,
    )
    db.add(plan)
    db.flush()
    rows = []
    for index, status in enumerate(installments, start=1):
        row = PayflowPaymentPlanInstallmentModel(
            plan_id=plan.id,
            sequence=index,
            amount=250.0,
            due_date=due or NOW.date(),  # the sweep works in UTC dates
            status=status,
        )
        db.add(row)
        rows.append(row)
    account.collection_status = "Payment Plan"
    db.commit()
    return plan, rows


def _admin(db):
    return db.query(UserModel).filter(UserModel.email == "admin@payflow.ai").one()


# --------------------------------------------------------------------- overpayment


def test_payment_larger_than_the_balance_only_recovers_what_was_owed(db_session):
    account, link = _account(db_session, outstanding=300.0)
    attempt = _attempt(db_session, account, link, 500.0)

    PayflowPaymentPortalService(db_session).apply_succeeded(attempt, provider_ref="pi_over")

    db_session.refresh(account)
    assert account.outstanding_balance == 0.0
    assert account.recovered_balance == 1000.0  # 700 before + the 300 still owed, not + 500


def test_overpayment_is_flagged_for_staff_with_the_excess_amount(db_session):
    account, link = _account(db_session, outstanding=300.0)
    attempt = _attempt(db_session, account, link, 500.0)

    PayflowPaymentPortalService(db_session).apply_succeeded(attempt, provider_ref="pi_over2")

    flagged = (
        db_session.query(PayflowOperationalExceptionModel)
        .filter_by(account_id=account.id)
        .all()
    )
    assert len(flagged) == 1
    assert "200.00" in flagged[0].reason
    assert "overpay" in flagged[0].reason.lower()


def test_other_open_checkout_is_cancelled_once_it_would_overpay(db_session):
    account, link = _account(db_session, outstanding=1000.0)
    full = _attempt(db_session, account, link, 1000.0, option="pay_in_full", status="Initiated")
    partial = _attempt(db_session, account, link, 500.0, status="Processing")

    PayflowPaymentPortalService(db_session).apply_succeeded(partial, provider_ref="pi_first")

    db_session.refresh(full)
    assert full.status == "Cancelled"  # 1000 no longer fits a 500 balance


def test_other_open_checkout_that_still_fits_is_left_open(db_session):
    account, link = _account(db_session, outstanding=1000.0)
    small = _attempt(db_session, account, link, 200.0, status="Initiated")
    partial = _attempt(db_session, account, link, 500.0, status="Processing")

    PayflowPaymentPortalService(db_session).apply_succeeded(partial, provider_ref="pi_first2")

    db_session.refresh(small)
    assert small.status == "Initiated"


# --------------------------------------------------------------------- refunds


def _paid(db, account, link, amount, ref):
    attempt = _attempt(db, account, link, amount)
    PayflowPaymentPortalService(db).apply_succeeded(attempt, provider_ref=ref)
    return (
        db.query(PayflowPaymentTransactionModel)
        .filter_by(payment_attempt_id=attempt.id, direction="payment")
        .one()
    )


def test_partial_refund_does_not_mark_the_whole_payment_refunded(db_session):
    account, link = _account(db_session, outstanding=1000.0)
    txn = _paid(db_session, account, link, 400.0, "pi_ref1")

    PayflowPaymentOutcomeService(db_session).apply_refund_or_reversal(
        original_txn_public_id=txn.public_id, amount=100.0, provider_ref="re_1"
    )

    db_session.refresh(txn)
    db_session.refresh(account)
    assert txn.status == "Succeeded"
    assert account.outstanding_balance == 700.0


def test_refunds_adding_up_to_the_payment_mark_it_refunded(db_session):
    account, link = _account(db_session, outstanding=1000.0)
    txn = _paid(db_session, account, link, 400.0, "pi_ref2")
    svc = PayflowPaymentOutcomeService(db_session)

    svc.apply_refund_or_reversal(original_txn_public_id=txn.public_id, amount=100.0, provider_ref="re_2a")
    svc.apply_refund_or_reversal(original_txn_public_id=txn.public_id, amount=300.0, provider_ref="re_2b")

    db_session.refresh(txn)
    assert txn.status == "Refunded"


def test_refunding_more_than_was_paid_in_total_is_refused(db_session):
    account, link = _account(db_session, outstanding=1000.0)
    txn = _paid(db_session, account, link, 400.0, "pi_ref3")
    svc = PayflowPaymentOutcomeService(db_session)
    svc.apply_refund_or_reversal(original_txn_public_id=txn.public_id, amount=300.0, provider_ref="re_3a")

    with pytest.raises(ValidationAppError):
        svc.apply_refund_or_reversal(
            original_txn_public_id=txn.public_id, amount=200.0, provider_ref="re_3b"
        )


# --------------------------------------------------------------------- auto-charge


def _charge_calls(db, plan_rows):
    """Run the sweep with Stripe faked; return the instalment sequences it tried to charge."""
    calls = []

    def fake_intent(**kwargs):
        calls.append(int(kwargs["metadata"]["installment_sequence"]))
        return {"id": f"pi_auto_{len(calls)}", "status": "processing"}

    with patch.object(plan_auto_charge_service, "stripe_configured", return_value=True), \
            patch.object(plan_auto_charge_service, "create_off_session_payment_intent", fake_intent):
        result = PayflowPlanAutoChargeService(db).charge_due_installments()
    return calls, result


def test_auto_charge_does_not_charge_an_account_on_hold(db_session):
    account, link = _account(db_session)
    _plan(db_session, account, link, installments=("Pending",))
    account.provincial_hold = True
    db_session.commit()

    calls, _result = _charge_calls(db_session, None)

    assert calls == []


def test_auto_charge_does_not_charge_a_cancelled_plan(db_session):
    account, link = _account(db_session)
    plan, _rows = _plan(db_session, account, link, installments=("Pending",))

    PayflowPaymentOutcomeService(db_session).cancel_plan(
        _admin(db_session), plan.public_id, reason="Customer disputes the debt"
    )
    calls, _result = _charge_calls(db_session, None)

    db_session.refresh(plan)
    db_session.refresh(account)
    assert calls == []
    assert plan.status == "Cancelled"
    assert plan.cancelled_at is not None
    assert plan.auto_charge_enabled is False
    assert account.collection_status == "Active"


def test_staff_can_pause_auto_charge_without_cancelling_the_plan(db_session):
    account, link = _account(db_session)
    plan, _rows = _plan(db_session, account, link, installments=("Pending",))

    PayflowPaymentOutcomeService(db_session).set_plan_auto_charge(
        _admin(db_session), plan.public_id, enabled=False
    )
    calls, _result = _charge_calls(db_session, None)

    db_session.refresh(plan)
    assert calls == []
    assert plan.status == "Active"
    assert plan.auto_charge_enabled is False


def test_failed_instalment_is_tried_again_after_a_day(db_session):
    account, link = _account(db_session)
    plan, rows = _plan(db_session, account, link, installments=("Failed",))
    _attempt(
        db_session, account, link, 250.0, option="pay_next_instalment", status="Failed",
        plan_id=plan.public_id,
        provider_payload={"auto_charge": True, "installment_sequence": 1},
        completed_at=NOW - timedelta(hours=30),
    )

    calls, _result = _charge_calls(db_session, None)

    assert calls == [1]


def test_failed_instalment_is_not_hammered_within_the_same_day(db_session):
    account, link = _account(db_session)
    plan, rows = _plan(db_session, account, link, installments=("Failed",))
    _attempt(
        db_session, account, link, 250.0, option="pay_next_instalment", status="Failed",
        plan_id=plan.public_id,
        provider_payload={"auto_charge": True, "installment_sequence": 1},
        completed_at=NOW - timedelta(hours=2),
    )

    calls, _result = _charge_calls(db_session, None)

    assert calls == []


def test_retry_uses_a_new_idempotency_key_so_stripe_does_not_replay_the_decline(db_session):
    account, link = _account(db_session)
    plan, rows = _plan(db_session, account, link, installments=("Failed",))
    first = _attempt(
        db_session, account, link, 250.0, option="pay_next_instalment", status="Failed",
        plan_id=plan.public_id,
        idempotency_key=f"autocharge_{plan.public_id}_1_{NOW.date().isoformat()}",
        provider_payload={"auto_charge": True, "installment_sequence": 1},
        completed_at=NOW - timedelta(hours=30),
    )

    _charge_calls(db_session, None)

    retry = (
        db_session.query(PayflowPaymentAttemptModel)
        .filter(PayflowPaymentAttemptModel.id != first.id, PayflowPaymentAttemptModel.plan_id == plan.public_id)
        .one()
    )
    assert retry.idempotency_key != first.idempotency_key


def test_failed_charge_puts_the_plan_overdue_and_tells_the_workflow(db_session):
    account, link = _account(db_session)
    plan, rows = _plan(db_session, account, link, installments=("Pending",))

    with patch.object(WorkflowExecutionService, "on_payment_event") as event:
        PayflowPaymentOutcomeService(db_session).record_auto_charge_failure(
            plan, installment=rows[0], detail="card_declined"
        )

    assert plan.status == "Overdue"
    assert rows[0].status == "Failed"
    statuses = [call.args[1].get("status") for call in event.call_args_list]
    assert "plan_missed" in statuses


def test_missed_instalment_event_is_not_recorded_as_a_payment(db_session):
    account, _link = _account(db_session)

    WorkflowExecutionService(db_session).on_payment_event(
        account.id, {"status": "plan_missed", "completed": False, "reason": "Instalment overdue"}
    )

    db_session.refresh(account)
    assert account.last_payment_date is None
    assert account.last_payment_amount is None


def test_missed_instalment_event_does_not_credit_a_message_with_a_payment(db_session):
    from app.modules.payflow.services.communication_service import PayflowCommunicationService

    account, _link = _account(db_session)

    with patch.object(PayflowCommunicationService, "mark_payment_event") as credited:
        WorkflowExecutionService(db_session).on_payment_event(
            account.id, {"status": "plan_missed", "completed": False}
        )

    credited.assert_not_called()


def test_auto_charge_charges_a_due_instalment_on_an_open_case(db_session):
    account, link = _account(db_session)
    _plan(db_session, account, link, installments=("Pending",))

    calls, _result = _charge_calls(db_session, None)

    assert calls == [1]


# --------------------------------------------------------------------- staff API


def test_api_cancels_a_payment_plan(client, admin_token, db_session):
    account, link = _account(db_session)
    plan, _rows = _plan(db_session, account, link, installments=("Pending", "Pending"))

    response = client.post(
        f"/payflow/payment-plans/{plan.public_id}/cancel",
        json={"reason": "Customer disputes the debt"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "Cancelled"


def test_api_pauses_auto_charge_on_a_payment_plan(client, admin_token, db_session):
    account, link = _account(db_session)
    plan, _rows = _plan(db_session, account, link, installments=("Pending",))

    response = client.post(
        f"/payflow/payment-plans/{plan.public_id}/auto-charge",
        json={"enabled": False},
        headers={"Authorization": f"Bearer {admin_token}"},
    )

    assert response.status_code == 200
    db_session.refresh(plan)
    assert plan.auto_charge_enabled is False


def test_api_plan_cancel_needs_a_login(client, db_session):
    account, link = _account(db_session)
    plan, _rows = _plan(db_session, account, link, installments=("Pending",))

    response = client.post(f"/payflow/payment-plans/{plan.public_id}/cancel", json={})

    assert response.status_code == 401


def test_api_unknown_plan_is_not_found(client, admin_token):
    response = client.post(
        "/payflow/payment-plans/does-not-exist/cancel",
        json={},
        headers={"Authorization": f"Bearer {admin_token}"},
    )

    assert response.status_code == 404
