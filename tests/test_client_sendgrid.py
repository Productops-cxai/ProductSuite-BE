"""Per-client SendGrid provider, sender identities, resolver, and From failover."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet

from app.core.config import settings
from app.core.exceptions import ValidationAppError
from app.core.secrets_crypto import decrypt_with_key, encrypt_value
from app.infrastructure.database.models import (
    PayflowAccountModel,
    PayflowAccountSenderOverrideModel,
    PayflowClientModel,
    PayflowClientSenderIdentityModel,
    PayflowCommunicationModel,
    PayflowPaymentTransactionModel,
)
from app.modules.payflow.services.communication_service import PayflowCommunicationService
from app.modules.payflow.services.email_provider_service import PayflowEmailProviderService
from app.modules.payflow.services.sender_identity_service import PayflowSenderIdentityService
from app.shared.enums import PayflowCommStatus


@pytest.fixture()
def fernet_key(monkeypatch):
    key = Fernet.generate_key().decode("ascii")
    monkeypatch.setenv("ENV_MASTER_KEY", key)
    return key


@pytest.fixture()
def first_party_client(db_session, fernet_key):
    client = PayflowClientModel(
        code="sg-fp-1",
        name="SendGrid First Party",
        status="active",
        client_type="first_party",
        channel_email=True,
        brand_name="Brand X",
        sender_name="Collections",
        email_from="legacy@clientx.com",
    )
    db_session.add(client)
    db_session.commit()
    db_session.refresh(client)
    return client


def _account(db, client, ref="SG-ACC-1"):
    acc = PayflowAccountModel(
        client_id=client.id,
        customer_name="Pat Customer",
        account_reference=ref,
        case_reference=f"CASE-{ref}",
        original_balance=100.0,
        outstanding_balance=100.0,
        collection_status="Active",
        email="pat@example.com",
        email_consent=True,
    )
    db.add(acc)
    db.commit()
    db.refresh(acc)
    return acc


def test_encrypt_decrypt_roundtrip(fernet_key):
    enc = encrypt_value("sg.secret.key", key=fernet_key)
    assert enc.startswith("enc:")
    assert decrypt_with_key(enc, key=fernet_key) == "sg.secret.key"


def test_provider_upsert_never_returns_raw_key(db_session, first_party_client, fernet_key):
    svc = PayflowEmailProviderService(db_session)
    status = svc.upsert(
        first_party_client.id,
        api_key="SG.TEST_API_KEY",
        webhook_verification_key="webhook-public-key",
        label="Client X SG",
    )
    assert status["configured"] is True
    assert status["has_api_key"] is True
    assert status["has_webhook_verification_key"] is True
    assert "api_key" not in status
    assert "SG.TEST" not in str(status)

    row = svc.get_provider_row(first_party_client.id)
    assert row is not None
    assert row.api_key_enc.startswith("enc:")
    assert svc.decrypt_api_key(first_party_client.id) == "SG.TEST_API_KEY"


def test_provider_test_connection_uses_http(db_session, first_party_client, fernet_key):
    svc = PayflowEmailProviderService(db_session)
    svc.upsert(first_party_client.id, api_key="SG.TEST")

    class _Resp:
        status_code = 200
        text = "{}"

    with patch("app.modules.payflow.services.email_provider_service.httpx.Client") as client_cls:
        client_cls.return_value.__enter__.return_value.get.return_value = _Resp()
        out = svc.test_connection(first_party_client.id)
    assert out["ok"] is True


def test_add_identity_with_mocked_dns(db_session, first_party_client, fernet_key):
    PayflowEmailProviderService(db_session).upsert(
        first_party_client.id, api_key="SG.TEST"
    )
    svc = PayflowSenderIdentityService(db_session)

    with patch(
        "app.modules.payflow.services.sender_identity_service.sendgrid_domain_auth.ensure_domain_auth",
        return_value={
            "sendgrid_domain_id": "99",
            "domain": "clientx.com",
            "valid": False,
            "dns_records": [
                {"type": "cname", "host": "em", "data": "sendgrid.net", "kind": "mail_cname"}
            ],
        },
    ):
        item = svc.add_identity(
            first_party_client.id,
            email="collections@clientx.com",
            display_name="Collections",
            is_primary=True,
        )

    assert item["email"] == "collections@clientx.com"
    assert item["is_primary"] is True
    assert item["verification_status"] == "dns_pending"
    assert item["dns_records"]
    db_session.refresh(first_party_client)
    assert first_party_client.email_from == "collections@clientx.com"


def test_resolve_uses_primary_verified_identity(db_session, first_party_client, fernet_key):
    PayflowEmailProviderService(db_session).upsert(
        first_party_client.id, api_key="SG.CLIENT_KEY"
    )
    ident = PayflowClientSenderIdentityModel(
        client_id=first_party_client.id,
        email="a@clientx.com",
        domain="clientx.com",
        is_enabled=True,
        is_primary=True,
        verification_status="verified",
        failover_order=10,
        display_name="A",
    )
    db_session.add(ident)
    db_session.commit()

    account = _account(db_session, first_party_client)
    resolved = PayflowSenderIdentityService(db_session).resolve_for_account(
        client=first_party_client, account=account
    )
    assert resolved.from_email == "a@clientx.com"
    assert resolved.api_key == "SG.CLIENT_KEY"
    assert resolved.identity_id == ident.id
    assert resolved.source == "identity"


def test_resolve_platform_for_third_party(db_session, fernet_key):
    client = PayflowClientModel(
        code="sg-tp-1",
        name="Third Party Co",
        status="active",
        client_type="third_party",
    )
    db_session.add(client)
    db_session.commit()
    with patch.object(settings, "SENDGRID_FROM_EMAIL", "ops@payflow.io"), patch.object(
        settings, "SENDGRID_API_KEY", "SG.PLATFORM"
    ):
        resolved = PayflowSenderIdentityService(db_session).resolve_for_account(
            client=client, account=None
        )
    assert resolved.from_email == "ops@payflow.io"
    assert resolved.api_key == "SG.PLATFORM"
    assert resolved.source == "platform"


def test_failover_without_payment_switches_from(db_session, first_party_client, fernet_key):
    PayflowEmailProviderService(db_session).upsert(
        first_party_client.id, api_key="SG.TEST"
    )
    a = PayflowClientSenderIdentityModel(
        client_id=first_party_client.id,
        email="a@clientx.com",
        domain="clientx.com",
        is_enabled=True,
        is_primary=True,
        verification_status="verified",
        failover_order=10,
    )
    b = PayflowClientSenderIdentityModel(
        client_id=first_party_client.id,
        email="b@clientx.com",
        domain="clientx.com",
        is_enabled=False,
        is_primary=False,
        verification_status="verified",
        failover_order=20,
    )
    db_session.add_all([a, b])
    db_session.commit()

    account = _account(db_session, first_party_client)
    comm = PayflowCommunicationModel(
        code="COM-SG-FAILOVER",
        client_id=first_party_client.id,
        account_id=account.id,
        channel="Email",
        purpose="Payment Reminder",
        status=PayflowCommStatus.SENT.value,
        from_email=a.email,
        sender_identity_id=a.id,
        balance=100.0,
    )
    db_session.add(comm)
    db_session.commit()

    result = PayflowCommunicationService(db_session).apply_provider_event(
        event_type="spamreport",
        communication_id=comm.id,
        provider_event_id="sg-evt-1",
        commit=True,
    )
    assert result.get("matched") is True

    db_session.refresh(account)
    assert account.email_consent is not False

    ov = (
        db_session.query(PayflowAccountSenderOverrideModel)
        .filter_by(account_id=account.id)
        .one()
    )
    assert ov.active_identity_id == b.id
    db_session.refresh(b)
    assert b.is_enabled is True

    resolved = PayflowSenderIdentityService(db_session).resolve_for_account(
        client=first_party_client, account=account
    )
    assert resolved.from_email == "b@clientx.com"


def test_failover_with_payment_sets_global_unsub(db_session, first_party_client, fernet_key):
    a = PayflowClientSenderIdentityModel(
        client_id=first_party_client.id,
        email="a@clientx.com",
        domain="clientx.com",
        is_enabled=True,
        is_primary=True,
        verification_status="verified",
        failover_order=10,
    )
    db_session.add(a)
    account = _account(db_session, first_party_client, ref="SG-PAID-1")
    db_session.add(
        PayflowPaymentTransactionModel(
            public_id="txn-sg-1",
            client_id=first_party_client.id,
            account_id=account.id,
            direction="payment",
            status="Succeeded",
            amount=50.0,
            currency_code="CAD",
            completed_at=datetime.now(timezone.utc),
        )
    )
    comm = PayflowCommunicationModel(
        code="COM-SG-PAID",
        client_id=first_party_client.id,
        account_id=account.id,
        channel="Email",
        purpose="Payment Reminder",
        status=PayflowCommStatus.SENT.value,
        sender_identity_id=a.id,
        balance=50.0,
    )
    db_session.add(comm)
    db_session.commit()

    PayflowCommunicationService(db_session).apply_provider_event(
        event_type="spamreport",
        communication_id=comm.id,
        provider_event_id="sg-evt-paid",
        commit=True,
    )
    db_session.refresh(account)
    assert account.email_consent is False
    assert (
        db_session.query(PayflowAccountSenderOverrideModel)
        .filter_by(account_id=account.id)
        .count()
        == 0
    )


def test_cannot_disable_primary(db_session, first_party_client, fernet_key):
    PayflowEmailProviderService(db_session).upsert(
        first_party_client.id, api_key="SG.TEST"
    )
    with patch(
        "app.modules.payflow.services.sender_identity_service.sendgrid_domain_auth.ensure_domain_auth",
        return_value={
            "sendgrid_domain_id": "1",
            "domain": "clientx.com",
            "valid": True,
            "dns_records": [],
        },
    ):
        item = PayflowSenderIdentityService(db_session).add_identity(
            first_party_client.id, email="only@clientx.com", is_primary=True
        )
    with pytest.raises(ValidationAppError):
        PayflowSenderIdentityService(db_session).toggle(
            first_party_client.id, item["id"], is_enabled=False
        )


def test_unverified_provider_resolves_unresolved(db_session, first_party_client, fernet_key):
    PayflowEmailProviderService(db_session).upsert(
        first_party_client.id, api_key="SG.TEST"
    )
    db_session.add(
        PayflowClientSenderIdentityModel(
            client_id=first_party_client.id,
            email="pending@clientx.com",
            domain="clientx.com",
            is_enabled=True,
            is_primary=True,
            verification_status="dns_pending",
            failover_order=10,
        )
    )
    db_session.commit()
    resolved = PayflowSenderIdentityService(db_session).resolve_for_account(
        client=first_party_client, account=None
    )
    assert resolved.source == "unresolved"
    assert resolved.from_email is None


def test_payment_clears_from_override(db_session, first_party_client, fernet_key):
    a = PayflowClientSenderIdentityModel(
        client_id=first_party_client.id,
        email="a@clientx.com",
        domain="clientx.com",
        is_enabled=True,
        is_primary=True,
        verification_status="verified",
        failover_order=10,
    )
    b = PayflowClientSenderIdentityModel(
        client_id=first_party_client.id,
        email="b@clientx.com",
        domain="clientx.com",
        is_enabled=True,
        is_primary=False,
        verification_status="verified",
        failover_order=20,
    )
    db_session.add_all([a, b])
    account = _account(db_session, first_party_client, ref="SG-CLR-1")
    db_session.commit()

    svc = PayflowSenderIdentityService(db_session)
    svc.apply_from_failover(account=account, blocked_identity_id=a.id, reason="spamreport")
    db_session.commit()
    assert svc.get_active_override(account.id) is not None

    assert svc.clear_override_for_account(account.id) is True
    db_session.commit()
    assert svc.get_active_override(account.id) is None


def test_backfill_creates_identity_from_email_from(db_session, first_party_client, fernet_key):
    first_party_client.email_from = "legacy@clientx.com"
    db_session.commit()
    created = PayflowSenderIdentityService(db_session).backfill_from_client_email_from(
        first_party_client.id
    )
    assert created is not None
    assert created["email"] == "legacy@clientx.com"
    assert created["is_primary"] is True
    # Second call is a no-op.
    assert (
        PayflowSenderIdentityService(db_session).backfill_from_client_email_from(
            first_party_client.id
        )
        is None
    )


def test_spam_event_payload_includes_from_email(db_session, first_party_client, fernet_key):
    a = PayflowClientSenderIdentityModel(
        client_id=first_party_client.id,
        email="a@clientx.com",
        domain="clientx.com",
        is_enabled=True,
        is_primary=True,
        verification_status="verified",
        failover_order=10,
    )
    db_session.add(a)
    account = _account(db_session, first_party_client, ref="SG-EVT-1")
    comm = PayflowCommunicationModel(
        code="COM-SG-FROM",
        client_id=first_party_client.id,
        account_id=account.id,
        channel="Email",
        purpose="Payment Reminder",
        status=PayflowCommStatus.SENT.value,
        from_email="a@clientx.com",
        sender_identity_id=None,  # set after flush
        balance=10.0,
    )
    db_session.add(comm)
    db_session.flush()
    comm.sender_identity_id = a.id
    db_session.commit()

    # Paid path so we don't need a second identity; still stores From on the event.
    db_session.add(
        PayflowPaymentTransactionModel(
            public_id="txn-sg-from",
            client_id=first_party_client.id,
            account_id=account.id,
            direction="payment",
            status="Succeeded",
            amount=1.0,
            currency_code="CAD",
        )
    )
    db_session.commit()

    PayflowCommunicationService(db_session).apply_provider_event(
        event_type="spamreport",
        communication_id=comm.id,
        provider_event_id="sg-evt-from",
        commit=True,
    )
    from app.infrastructure.database.models import PayflowCommunicationEventModel

    ev = (
        db_session.query(PayflowCommunicationEventModel)
        .filter_by(communication_id=comm.id, event_type="spamreport")
        .one()
    )
    assert (ev.payload or {}).get("from_email") == "a@clientx.com"
    assert (ev.payload or {}).get("sender_identity_id") == a.id

