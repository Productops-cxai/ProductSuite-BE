"""Security gaps: private import files, link hosts, client scope, webhooks, throttling."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from app.core import rate_limit
from app.core.config import settings
from app.core.request_context import frontend_base_from_headers
from app.core.security import hash_password
from app.infrastructure.database.models import (
    EmailLogModel,
    OrganizationModel,
    PayflowAccountModel,
    PayflowClientModel,
    PayflowImportJobModel,
    PayflowImportRunModel,
    PayflowPaymentLinkModel,
    PayflowPermissionModel,
    PayflowPortfolioModel,
    PayflowRoleModel,
    PayflowRolePermissionModel,
    PayflowUserClientAssignmentModel,
    PayflowUserMembershipModel,
    ProductModel,
    RoleModel,
    UserModel,
    UserProductModel,
)
from app.shared.enums import PayflowRoleCode, PersonStatus, PlatformRole

UPLOADS = Path(__file__).resolve().parents[1] / "uploads"
SCOPED_PERMISSIONS = [
    "view_client",
    "edit_client",
    "delete_client",
    "create_client",
    "import_accounts",
    "view_collection_cases",
    "view_customer_accounts",
]


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def two_clients(db_session):
    mine = PayflowClientModel(code="sec-mine", name="My Client", status="active")
    other = PayflowClientModel(code="sec-other", name="Other Client", status="active")
    db_session.add_all([mine, other])
    db_session.flush()
    portfolio = PayflowPortfolioModel(client_id=other.id, name="Other Portfolio", code="sec-o-p")
    db_session.add(portfolio)
    db_session.commit()
    return mine, other, portfolio


@pytest.fixture()
def scoped_token(client, db_session, two_clients):
    """A supervisor who may edit / delete / import — but only for 'My Client'."""
    mine, _other, _portfolio = two_clients
    org = db_session.query(OrganizationModel).first()
    role = db_session.query(RoleModel).filter_by(code=PlatformRole.PLATFORM_USER.value).one()
    user = UserModel(
        full_name="Scoped Supervisor",
        email="scoped@example.com",
        password_hash=hash_password("Scoped@12345"),
        organization_id=org.id,
        role_id=role.id,
        status=PersonStatus.ACTIVE.value,
    )
    db_session.add(user)
    db_session.flush()
    payflow = db_session.query(ProductModel).filter_by(code="PAYFLOW").one()
    db_session.add(UserProductModel(user_id=user.id, product_id=payflow.id))
    supervisor = (
        db_session.query(PayflowRoleModel)
        .filter(PayflowRoleModel.code == PayflowRoleCode.SUPERVISOR.value)
        .one()
    )
    for code in SCOPED_PERMISSIONS:
        permission = PayflowPermissionModel(code=code, name=code, group_key="test")
        db_session.add(permission)
        db_session.flush()
        db_session.add(
            PayflowRolePermissionModel(payflow_role_id=supervisor.id, permission_id=permission.id)
        )
    membership = PayflowUserMembershipModel(user_id=user.id, payflow_role_id=supervisor.id)
    db_session.add(membership)
    db_session.flush()
    db_session.add(
        PayflowUserClientAssignmentModel(membership_id=membership.id, client_id=mine.id)
    )
    db_session.commit()
    response = client.post(
        "/auth/login", json={"email": "scoped@example.com", "password": "Scoped@12345"}
    )
    assert response.status_code == 200
    return response.json()["access_token"]


# ------------------------------------------------------------ #14 private import files


def test_import_files_are_not_served_from_uploads(client):
    folder = UPLOADS / "imports"
    folder.mkdir(parents=True, exist_ok=True)
    secret = folder / "sec-test-debtors.csv"
    secret.write_text("name,email\nJane,jane@example.com\n")
    try:
        response = client.get("/uploads/imports/sec-test-debtors.csv")
    finally:
        secret.unlink()

    assert response.status_code == 404


def test_import_job_files_are_not_served_from_uploads(client):
    folder = UPLOADS / "import_jobs"
    folder.mkdir(parents=True, exist_ok=True)
    secret = folder / "sec-test-job.csv"
    secret.write_text("name\nJane\n")
    try:
        response = client.get("/uploads/import_jobs/sec-test-job.csv")
    finally:
        secret.unlink()

    assert response.status_code == 404


def test_avatars_and_logos_are_still_served(client):
    folder = UPLOADS / "avatars"
    folder.mkdir(parents=True, exist_ok=True)
    picture = folder / "sec-test-avatar.txt"
    picture.write_text("ok")
    try:
        response = client.get("/uploads/avatars/sec-test-avatar.txt")
    finally:
        picture.unlink()

    assert response.status_code == 200


# ------------------------------------------------------------ #15 link host allowlist


def test_unknown_origin_is_not_used_for_email_links():
    assert frontend_base_from_headers("https://evil.example", None) is None
    assert frontend_base_from_headers(None, "https://evil.example/login") is None


def test_configured_frontend_origin_is_used_for_email_links():
    allowed = settings.cors_allow_origins()[0]
    assert frontend_base_from_headers(allowed, None) == allowed.rstrip("/")


def test_forgot_password_from_a_forged_origin_links_to_the_real_site(client, db_session):
    client.post(
        "/auth/forgot-password",
        json={"email": "ops@example.com"},
        headers={"Origin": "https://evil.example"},
    )

    log = db_session.query(EmailLogModel).order_by(EmailLogModel.id.desc()).first()
    assert log is not None
    assert "evil.example" not in (log.action_link or "")
    assert (log.action_link or "").startswith(settings.FRONTEND_URL.rstrip("/"))


# ------------------------------------------------------------ #16 client scope


def test_scoped_user_cannot_activate_another_client(client, scoped_token, two_clients):
    _mine, other, _portfolio = two_clients
    response = client.post(f"/payflow/clients/{other.id}/activate", headers=_auth(scoped_token))
    assert response.status_code == 403


def test_scoped_user_cannot_add_a_portfolio_to_another_client(client, scoped_token, two_clients):
    _mine, other, _portfolio = two_clients
    response = client.post(
        f"/payflow/clients/{other.id}/portfolios",
        json={"name": "Sneaky", "code": "sneaky"},
        headers=_auth(scoped_token),
    )
    assert response.status_code == 403


def test_scoped_user_cannot_edit_another_clients_portfolio(client, scoped_token, two_clients):
    _mine, other, portfolio = two_clients
    response = client.post(
        f"/payflow/clients/{other.id}/portfolios/{portfolio.id}/update",
        json={"name": "Renamed"},
        headers=_auth(scoped_token),
    )
    assert response.status_code == 403


def test_scoped_user_cannot_delete_another_client(client, scoped_token, two_clients, db_session):
    _mine, other, _portfolio = two_clients
    response = client.post(f"/payflow/clients/{other.id}/delete", headers=_auth(scoped_token))
    assert response.status_code == 403
    assert db_session.get(PayflowClientModel, other.id) is not None


def test_scoped_user_cannot_delete_another_clients_portfolio(client, scoped_token, two_clients):
    _mine, other, portfolio = two_clients
    response = client.post(
        f"/payflow/clients/{other.id}/portfolios/{portfolio.id}/delete",
        headers=_auth(scoped_token),
    )
    assert response.status_code == 403


def test_scoped_user_cannot_remove_another_clients_logo(client, scoped_token, two_clients):
    _mine, other, _portfolio = two_clients
    response = client.post(f"/payflow/clients/{other.id}/logo/delete", headers=_auth(scoped_token))
    assert response.status_code == 403


def test_scoped_user_can_still_manage_their_own_client(client, scoped_token, two_clients):
    mine, _other, _portfolio = two_clients
    response = client.post(
        f"/payflow/clients/{mine.id}/portfolios",
        json={"name": "Mine", "code": "sec-mine-p"},
        headers=_auth(scoped_token),
    )
    assert response.status_code == 200


def _import_run(db, user_id, name):
    run = PayflowImportRunModel(
        kind="accounts",
        file_name=name,
        uploaded_by_user_id=user_id,
        uploaded_by_name="Someone",
        status="Completed",
    )
    db.add(run)
    db.commit()
    return run


def test_scoped_user_only_sees_their_own_imports(client, scoped_token, db_session):
    admin = db_session.query(UserModel).filter_by(email="admin@payflow.ai").one()
    scoped = db_session.query(UserModel).filter_by(email="scoped@example.com").one()
    theirs = _import_run(db_session, admin.id, "admin-file.xlsx")
    mine = _import_run(db_session, scoped.id, "my-file.xlsx")

    listing = client.get("/payflow/imports", headers=_auth(scoped_token))
    assert listing.status_code == 200
    assert [row["id"] for row in listing.json()["imports"]] == [mine.id]

    other = client.get(f"/payflow/imports/{theirs.id}", headers=_auth(scoped_token))
    assert other.status_code == 404


def test_operations_admin_sees_every_import(client, admin_token, scoped_token, db_session):
    scoped = db_session.query(UserModel).filter_by(email="scoped@example.com").one()
    run = _import_run(db_session, scoped.id, "my-file.xlsx")

    response = client.get(f"/payflow/imports/{run.id}", headers=_auth(admin_token))

    assert response.status_code == 200


def test_scoped_user_cannot_poll_someone_elses_import_job(client, scoped_token, db_session):
    admin = db_session.query(UserModel).filter_by(email="admin@payflow.ai").one()
    job = PayflowImportJobModel(
        job_type="account_upload", status="running", file_name="x.xlsx",
        created_by_user_id=admin.id,
    )
    db_session.add(job)
    db_session.commit()

    response = client.get(f"/payflow/import-jobs/{job.id}", headers=_auth(scoped_token))

    assert response.status_code == 404


# ------------------------------------------------------------ #17 SendGrid webhook


def test_sendgrid_webhook_refuses_unsigned_events_in_production(client):
    with patch.object(settings, "APP_ENV", "production"), patch.object(
        settings, "SENDGRID_WEBHOOK_VERIFICATION_KEY", ""
    ):
        response = client.post("/webhooks/sendgrid", json=[])

    assert response.status_code == 401


def test_sendgrid_webhook_still_accepts_unsigned_events_in_development(client):
    with patch.object(settings, "APP_ENV", "development"), patch.object(
        settings, "SENDGRID_WEBHOOK_VERIFICATION_KEY", ""
    ):
        response = client.post("/webhooks/sendgrid", json=[])

    assert response.status_code == 200


# ------------------------------------------------------------ #18 manual payment event


def _account(db, client_row):
    account = PayflowAccountModel(
        client_id=client_row.id,
        customer_name="Jane Doe",
        customer_first_name="Jane",
        account_reference="SEC-ACC-0001",
        case_reference="CASE-SEC-1",
        original_balance=500.0,
        outstanding_balance=500.0,
        collection_status="Active",
    )
    db.add(account)
    db.commit()
    return account


def test_case_viewer_cannot_post_a_payment_event(client, scoped_token, two_clients, db_session):
    mine, _other, _portfolio = two_clients
    account = _account(db_session, mine)

    response = client.post(
        f"/payflow/accounts/{account.id}/workflow-events/payment",
        json={"amount": 500, "completed": True},
        headers=_auth(scoped_token),
    )

    assert response.status_code == 403
    db_session.refresh(account)
    assert account.last_payment_amount is None


def test_operations_admin_can_post_a_payment_event(client, admin_token, two_clients, db_session):
    mine, _other, _portfolio = two_clients
    account = _account(db_session, mine)

    response = client.post(
        f"/payflow/accounts/{account.id}/workflow-events/payment",
        json={"amount": 100},
        headers=_auth(admin_token),
    )

    assert response.status_code == 200


# ------------------------------------------------------------ #19 throttling, links, PII


def test_login_is_throttled_after_too_many_attempts(client):
    limit = rate_limit.LIMITS["login"][0]
    for _ in range(limit):
        client.post("/auth/login", json={"email": "ops@example.com", "password": "wrong"})

    response = client.post("/auth/login", json={"email": "ops@example.com", "password": "wrong"})

    assert response.status_code == 429
    assert "Retry-After" in response.headers


def test_forgot_password_is_throttled(client):
    limit = rate_limit.LIMITS["password_reset"][0]
    for _ in range(limit):
        client.post("/auth/forgot-password", json={"email": "ops@example.com"})

    response = client.post("/auth/forgot-password", json={"email": "ops@example.com"})

    assert response.status_code == 429


def test_public_payment_link_lookups_are_throttled(client):
    limit = rate_limit.LIMITS["public_token"][0]
    for _ in range(limit):
        client.get("/payflow/public/payment-links/not-a-real-token")

    response = client.get("/payflow/public/payment-links/not-a-real-token")

    assert response.status_code == 429


def test_ordinary_requests_are_not_throttled(client, admin_token):
    for _ in range(40):
        assert client.get("/auth/me", headers=_auth(admin_token)).status_code == 200


def _link(db, account, *, status="Active", expires_in_days=7):
    link = PayflowPaymentLinkModel(
        token=f"sec-token-{status.lower()}",
        client_id=account.client_id,
        account_id=account.id,
        status=status,
        expires_at=datetime.now(timezone.utc) + timedelta(days=expires_in_days),
    )
    db.add(link)
    db.commit()
    return link


def test_revoked_payment_link_shows_no_customer_details(client, two_clients, db_session):
    mine, _other, _portfolio = two_clients
    account = _account(db_session, mine)
    link = _link(db_session, account, status="Revoked")

    body = client.get(f"/payflow/public/payment-links/{link.token}").json()

    assert body["usable"] is False
    assert "Jane" not in str(body)
    assert "SEC-ACC-0001" not in str(body)
    assert body["case"]["outstanding_balance"] == 0


def test_expired_payment_link_shows_no_customer_details(client, two_clients, db_session):
    mine, _other, _portfolio = two_clients
    account = _account(db_session, mine)
    link = _link(db_session, account, expires_in_days=-1)

    body = client.get(f"/payflow/public/payment-links/{link.token}").json()

    assert body["usable"] is False
    assert "Jane" not in str(body)
    assert "SEC-ACC-0001" not in str(body)


def test_usable_payment_link_never_sends_the_full_account_reference(client, two_clients, db_session):
    mine, _other, _portfolio = two_clients
    account = _account(db_session, mine)
    link = _link(db_session, account)

    body = client.get(f"/payflow/public/payment-links/{link.token}").json()

    assert "SEC-ACC-0001" not in str(body)
    assert body["customer"]["display_name"] == "Jane"


def test_resent_invitation_does_not_hand_back_the_activation_link_in_production(
    client, admin_token, db_session
):
    org = db_session.query(OrganizationModel).first()
    role = db_session.query(RoleModel).filter_by(code=PlatformRole.PLATFORM_USER.value).one()
    invited = UserModel(
        full_name="Invited Admin",
        email="invited@example.com",
        password_hash=hash_password("Temp@12345"),
        organization_id=org.id,
        role_id=role.id,
        status=PersonStatus.INVITED.value,
    )
    db_session.add(invited)
    db_session.flush()
    ops = db_session.query(PayflowRoleModel).filter_by(code=PayflowRoleCode.OPERATIONS_ADMIN.value).one()
    db_session.add(PayflowUserMembershipModel(user_id=invited.id, payflow_role_id=ops.id))
    db_session.commit()

    with patch.object(settings, "APP_ENV", "production"):
        response = client.post(
            f"/payflow/users/{invited.id}/resend-invitation", headers=_auth(admin_token)
        )

    assert response.status_code == 200
    assert "token=" not in response.text


def test_account_import_rows_for_a_client_the_user_cannot_import_for_are_refused(
    scoped_token, two_clients, db_session
):
    from app.core.exceptions import ValidationAppError
    from app.modules.payflow.services.import_service import PayflowImportService

    mine, other, _portfolio = two_clients
    scoped = db_session.query(UserModel).filter_by(email="scoped@example.com").one()
    svc = PayflowImportService(db_session)
    allowed: dict = {}

    svc._assert_may_import_for(scoped, mine, allowed)  # own client: fine
    with pytest.raises(ValidationAppError):
        svc._assert_may_import_for(scoped, other, allowed)


def test_operations_admin_may_import_for_any_client(two_clients, db_session):
    from app.modules.payflow.services.import_service import PayflowImportService

    _mine, other, _portfolio = two_clients
    admin = db_session.query(UserModel).filter_by(email="admin@payflow.ai").one()

    PayflowImportService(db_session)._assert_may_import_for(admin, other, {})


# ------------------------------------------------------------ AI job status for PayFlow users


def _ai_job(db, client_row, *, status="running"):
    from app.infrastructure.database.models import PayflowAiSuggestJobModel

    job = PayflowAiSuggestJobModel(
        client_id=client_row.id, portfolio_id=None, trigger="import", status=status
    )
    db.add(job)
    db.commit()
    return job


def test_payflow_user_sees_ai_suggest_status_for_their_client(
    client, scoped_token, two_clients, db_session
):
    mine, _other, _portfolio = two_clients
    job = _ai_job(db_session, mine)

    response = client.get(
        f"/payflow/ai-suggest-jobs?client_id={mine.id}", headers=_auth(scoped_token)
    )

    assert response.status_code == 200
    assert [row["id"] for row in response.json()] == [job.id]
    assert response.json()[0]["status"] == "running"


def test_payflow_user_cannot_see_ai_suggest_status_of_another_client(
    client, scoped_token, two_clients, db_session
):
    _mine, other, _portfolio = two_clients
    _ai_job(db_session, other)

    response = client.get(
        f"/payflow/ai-suggest-jobs?client_id={other.id}", headers=_auth(scoped_token)
    )

    assert response.status_code == 403


def test_human_review_reason_is_kept_on_a_workflow_step():
    from app.modules.payflow.schemas import StrategyNodeConfig

    config = StrategyNodeConfig(review_reason="Payment arrangement exception")

    assert config.model_dump(exclude_none=True) == {
        "review_reason": "Payment arrangement exception"
    }


def test_checkout_idempotency_key_never_returns_another_customers_payment(
    two_clients, db_session
):
    from app.modules.payflow.services.payment_portal_service import PayflowPaymentPortalService

    mine, other, _portfolio = two_clients
    first = _account(db_session, mine)
    second = PayflowAccountModel(
        client_id=other.id,
        customer_name="John Roe",
        account_reference="SEC-ACC-0002",
        case_reference="CASE-SEC-2",
        original_balance=900.0,
        outstanding_balance=900.0,
        collection_status="Active",
    )
    db_session.add(second)
    db_session.commit()
    link_a = _link(db_session, first)
    link_b = PayflowPaymentLinkModel(
        token="sec-token-b",
        client_id=second.client_id,
        account_id=second.id,
        status="Active",
        expires_at=datetime.now(timezone.utc) + timedelta(days=7),
    )
    db_session.add(link_b)
    db_session.commit()
    svc = PayflowPaymentPortalService(db_session)
    option_a = svc.resolve_portal(link_a.token)["payment_options"][0]["id"]
    option_b = svc.resolve_portal(link_b.token)["payment_options"][0]["id"]

    paid_a = svc.initiate(link_a.token, option_id=option_a, idempotency_key="same-key")
    paid_b = svc.initiate(link_b.token, option_id=option_b, idempotency_key="same-key")

    assert paid_b["id"] != paid_a["id"]
    assert paid_b["amount"] == 900.0
