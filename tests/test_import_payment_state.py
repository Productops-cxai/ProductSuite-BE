"""The daily CRM file must not wipe what PayFlow knows: payments, plans, reviews, opt-outs."""

from datetime import date, datetime, timedelta, timezone

from app.infrastructure.database.models import (
    PayflowAccountModel,
    PayflowClientModel,
    PayflowHumanReviewModel,
    PayflowPaymentPlanModel,
    PayflowPaymentTransactionModel,
)
from app.modules.payflow.services.import_service import PayflowImportService

_SEQ = 0


class Parsed(dict):
    """A parsed import row; columns the test does not set are empty."""

    def __missing__(self, key):
        return None


def _row(account, **over):
    base = Parsed(
        account_id=account.account_reference,
        customer_name=account.customer_name,
        customer_first_name="Test",
        customer_last_name="User",
        outstanding_balance=1000.0,
        original_balance=1000.0,
        collection_status="Active",
        account_status="OPEN",
        language="EN",
        currency_code="CAD",
        country_code="CA",
        email="a@example.com",
    )
    base.update(over)
    return base


def _account(db, *, outstanding=1000.0, status="Active"):
    """An account as the first import leaves it: CRM says 1000 owed."""
    global _SEQ
    _SEQ += 1
    client = PayflowClientModel(code=f"imp-c-{_SEQ}", name=f"Import Client {_SEQ}")
    db.add(client)
    db.flush()
    seed = PayflowAccountModel(
        client_id=client.id,
        customer_name="Test User",
        account_reference=f"IMP-{_SEQ}",
        case_reference=f"CASE-IMP-{_SEQ}",
    )
    svc = PayflowImportService(db)
    assert svc._upsert(None, client, None, _row(seed, outstanding_balance=outstanding,
                                               original_balance=outstanding,
                                               collection_status=status)) == "created"
    account = db.query(PayflowAccountModel).filter_by(account_reference=seed.account_reference).one()
    db.commit()
    return client, account


def _pay(db, account, amount, *, at=None):
    """What a verified PayFlow payment does: ledger row + balance moved."""
    global _SEQ
    _SEQ += 1
    before = float(account.outstanding_balance)
    account.outstanding_balance = max(0.0, before - amount)
    account.recovered_balance = float(account.recovered_balance or 0) + amount
    if account.outstanding_balance <= 0:
        account.collection_status = "Resolved"
    db.add(
        PayflowPaymentTransactionModel(
            public_id=f"txn-imp-{_SEQ}",
            client_id=account.client_id,
            account_id=account.id,
            direction="payment",
            status="Succeeded",
            amount=amount,
            balance_before=before,
            balance_after=account.outstanding_balance,
            completed_at=at or datetime.now(timezone.utc),
        )
    )
    db.commit()


def _import(db, client, account, **over):
    result = PayflowImportService(db)._upsert(account, client, None, _row(account, **over))
    db.commit()
    db.refresh(account)
    return result


def test_payment_survives_next_file_when_crm_balance_has_not_moved(db_session):
    client, account = _account(db_session)
    _pay(db_session, account, 400.0)

    _import(db_session, client, account, outstanding_balance=1000.0)

    assert account.outstanding_balance == 600.0
    assert account.recovered_balance == 400.0


def test_payment_is_not_subtracted_twice_once_crm_has_posted_it(db_session):
    client, account = _account(db_session)
    _pay(db_session, account, 400.0)

    _import(db_session, client, account, outstanding_balance=600.0)
    assert account.outstanding_balance == 600.0

    # And it stays posted on the files that follow.
    _import(db_session, client, account, outstanding_balance=600.0, email="b@example.com")
    assert account.outstanding_balance == 600.0
    assert account.recovered_balance == 400.0


def test_unposted_payment_still_counts_when_crm_adds_a_fee(db_session):
    client, account = _account(db_session)
    _pay(db_session, account, 400.0)

    _import(db_session, client, account, outstanding_balance=1050.0)

    assert account.outstanding_balance == 650.0


def test_crm_reduction_smaller_than_the_payment_does_not_absorb_it(db_session):
    client, account = _account(db_session)
    _pay(db_session, account, 400.0)

    # CRM took 100 off for its own reasons; our 400 is still not in its number.
    _import(db_session, client, account, outstanding_balance=900.0)

    assert account.outstanding_balance == 500.0


def test_payment_made_after_the_crm_snapshot_is_never_treated_as_posted(db_session):
    client, account = _account(db_session)
    snapshot = datetime.now(timezone.utc) - timedelta(hours=6)
    _pay(db_session, account, 400.0)  # paid now, after the CRM extract was taken

    # The CRM number dropped by exactly 400 for an unrelated reason before our payment.
    _import(db_session, client, account, outstanding_balance=600.0, source_updated_at=snapshot)

    assert account.outstanding_balance == 200.0


def test_account_paid_in_full_in_payflow_is_not_reopened_by_the_file(db_session):
    client, account = _account(db_session)
    _pay(db_session, account, 1000.0)
    assert account.collection_status == "Resolved"

    _import(db_session, client, account, outstanding_balance=1000.0, collection_status="Active")

    assert account.outstanding_balance == 0.0
    assert account.collection_status == "Resolved"


def test_account_on_a_payflow_plan_stays_on_payment_plan(db_session):
    client, account = _account(db_session)
    account.collection_status = "Payment Plan"
    db_session.add(
        PayflowPaymentPlanModel(
            public_id="plan-imp-1",
            client_id=client.id,
            account_id=account.id,
            status="Active",
            installments_total=4,
            installment_amount=250.0,
            total_payable=1000.0,
            start_date=date.today(),
        )
    )
    db_session.commit()

    _import(db_session, client, account, collection_status="Active", email="b@example.com")

    assert account.collection_status == "Payment Plan"


def test_open_human_review_is_not_cleared_by_the_file(db_session):
    client, account = _account(db_session)
    account.collection_status = "Human Review"
    account.human_review = True
    db_session.add(
        PayflowHumanReviewModel(
            code="HR-IMP-1",
            client_id=client.id,
            account_id=account.id,
            status="Awaiting Review",
            reason="Payment arrangement exception",
            proposed_action="Move to stronger collection treatment",
        )
    )
    db_session.commit()

    _import(db_session, client, account, collection_status="Active", email="b@example.com")

    assert account.human_review is True
    assert account.collection_status == "Human Review"


def test_unsubscribe_is_not_undone_by_a_file_that_says_consent_yes(db_session):
    client, account = _account(db_session)
    account.email_consent = False  # customer clicked unsubscribe
    db_session.commit()

    _import(db_session, client, account, email_consent=True, email="b@example.com")
    assert account.email_consent is False

    _import(db_session, client, account, email_consent=None, email="c@example.com")
    assert account.email_consent is False


def test_file_can_still_withdraw_consent(db_session):
    client, account = _account(db_session)
    account.email_consent = True
    db_session.commit()

    _import(db_session, client, account, email_consent=False)

    assert account.email_consent is False


def test_crm_balance_change_without_any_payflow_payment_is_taken_as_is(db_session):
    client, account = _account(db_session)

    _import(db_session, client, account, outstanding_balance=750.0)

    assert account.outstanding_balance == 750.0
    assert account.recovered_balance == 250.0


def test_refund_of_an_unposted_payment_restores_the_balance_after_the_next_file(db_session):
    client, account = _account(db_session)
    _pay(db_session, account, 400.0)
    # Staff refunds 150 of it: PayFlow's own balance goes back up to 750.
    account.outstanding_balance = 750.0
    account.recovered_balance = 250.0
    db_session.add(
        PayflowPaymentTransactionModel(
            public_id="txn-imp-refund",
            client_id=account.client_id,
            account_id=account.id,
            direction="refund",
            status="Refunded",
            amount=150.0,
            completed_at=datetime.now(timezone.utc),
        )
    )
    db_session.commit()

    _import(db_session, client, account, outstanding_balance=1000.0, email="b@example.com")

    assert account.outstanding_balance == 750.0
