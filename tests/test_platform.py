from datetime import datetime, timezone

from app.infrastructure.database.models import (
    OrganizationModel,
    OrganizationProductModel,
    ProductModel,
    UserModel,
    UserProductModel,
)
from app.modules.platform.services.entitlement_service import get_effective_products, has_product_access
from app.shared.enums import EntitlementStatus


def test_login_invalid_credentials(client):
    response = client.post(
        "/auth/login",
        json={"email": "admin@payflow.ai", "password": "wrong"},
    )
    assert response.status_code == 401


def test_login_success_and_me(client, admin_token):
    me = client.get("/auth/me", headers={"Authorization": f"Bearer {admin_token}"})
    assert me.status_code == 200
    body = me.json()
    assert body["user"]["email"] == "admin@payflow.ai"
    assert body["next_step"] == "platform_admin"
    assert len(body["products"]) == 2


def test_overview_kpis_require_admin(client, admin_token, user_token):
    denied = client.get(
        "/platform/overview",
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert denied.status_code == 403

    ok = client.get(
        "/platform/overview",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert ok.status_code == 200
    data = ok.json()
    assert data["registered_products_count"] == 2
    assert data["active_products_count"] == 2
    assert data["organizations_with_access_label"] == "1 of 1"


def test_menus_include_coming_soon(client, admin_token):
    response = client.get(
        "/menus?context=platform_admin",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert response.status_code == 200
    sections = response.json()["sections"]
    future = next(s for s in sections if s["key"] == "future")
    billing = future["items"][0]
    assert billing["is_coming_soon"] is True
    assert billing["badge"] == "Soon"


def test_two_layer_entitlement(db_session):
    user = db_session.query(UserModel).filter_by(email="ops@example.com").first()
    products = get_effective_products(db_session, user)
    assert [p.code for p in products] == ["PAYFLOW"]
    assert has_product_access(db_session, user, "PAYFLOW") is True
    assert has_product_access(db_session, user, "INSIGHTIQ") is False


def test_revoke_blocks_access(client, db_session, user_token):
    org = db_session.query(OrganizationModel).first()
    payflow = db_session.query(ProductModel).filter_by(code="PAYFLOW").first()
    ent = (
        db_session.query(OrganizationProductModel)
        .filter_by(organization_id=org.id, product_id=payflow.id)
        .first()
    )
    ent.status = EntitlementStatus.REVOKED.value
    ent.revoked_at = datetime.now(timezone.utc)
    db_session.commit()

    denied = client.get(
        "/payflow/health",
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert denied.status_code == 403

    products = client.get(
        "/me/products",
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert products.status_code == 200
    assert products.json() == []


def test_enter_product_and_payflow_gate(client, user_token):
    entered = client.post(
        "/products/PAYFLOW/enter",
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert entered.status_code == 200
    assert entered.json()["product"]["code"] == "PAYFLOW"

    health = client.get(
        "/payflow/health",
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert health.status_code == 200


def test_person_assignment_without_org_grant_not_enough(db_session):
    user = db_session.query(UserModel).filter_by(email="ops@example.com").first()
    insight = db_session.query(ProductModel).filter_by(code="INSIGHTIQ").first()
    db_session.add(UserProductModel(user_id=user.id, product_id=insight.id))
    ent = (
        db_session.query(OrganizationProductModel)
        .filter_by(organization_id=user.organization_id, product_id=insight.id)
        .first()
    )
    ent.status = EntitlementStatus.REVOKED.value
    db_session.commit()

    assert has_product_access(db_session, user, "INSIGHTIQ") is False


def test_remove_assignment_hides_product_from_me(client, admin_token, db_session):
    admin = db_session.query(UserModel).filter_by(email="admin@payflow.ai").first()
    payflow = db_session.query(ProductModel).filter_by(code="PAYFLOW").first()

    before = client.get("/me/products", headers={"Authorization": f"Bearer {admin_token}"})
    assert before.status_code == 200
    assert "PAYFLOW" in {p["code"] for p in before.json()}

    removed = client.post(
        "/people/remove-product",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"user_id": str(admin.id), "product_id": payflow.id},
    )
    assert removed.status_code == 200
    assert "PAYFLOW" not in {p["code"] for p in removed.json()["assigned_products"]}

    after = client.get("/me/products", headers={"Authorization": f"Bearer {admin_token}"})
    assert after.status_code == 200
    assert "PAYFLOW" not in {p["code"] for p in after.json()}

    response = client.post(
        "/products",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={
            "name": "RiskShield",
            "code": "RISKSHIELD",
            "description": "Future risk product",
            "status": "draft",
        },
    )
    assert response.status_code == 200
    assert response.json()["code"] == "RISKSHIELD"


def test_forgot_password_privacy(client):
    response = client.post(
        "/auth/forgot-password",
        json={"email": "missing@example.com"},
    )
    assert response.status_code == 200
    assert "If an account exists" in response.json()["message"]


def test_get_product_details(client, admin_token, db_session):
    payflow = db_session.query(ProductModel).filter_by(code="PAYFLOW").first()
    response = client.get(
        f"/products/{payflow.id}",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["code"] == "PAYFLOW"
    assert data["name"] == "PayFlow"
    assert data["status"] == "active"
    assert data["description"]
    assert "Collections" in data["description"]


def test_update_product_description_and_status(client, admin_token, db_session):
    payflow = db_session.query(ProductModel).filter_by(code="PAYFLOW").first()
    response = client.post(
        "/products",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={
            "id": payflow.id,
            "name": "PayFlow",
            "description": "Updated collections platform",
            "status": "active",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["description"] == "Updated collections platform"
    assert body["status"] == "active"
    assert body["code"] == "PAYFLOW"


def test_product_code_immutable_on_update(client, admin_token, db_session):
    payflow = db_session.query(ProductModel).filter_by(code="PAYFLOW").first()
    response = client.post(
        "/products",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={
            "id": payflow.id,
            "name": "PayFlow",
            "code": "NEWCODE",
            "status": "active",
        },
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "Product code cannot be changed"


def test_products_require_admin(client, user_token, db_session):
    payflow = db_session.query(ProductModel).filter_by(code="PAYFLOW").first()
    post_denied = client.post(
        "/products",
        headers={"Authorization": f"Bearer {user_token}"},
        json={"name": "Blocked Product", "code": "BLOCKED"},
    )
    assert post_denied.status_code == 403

    get_denied = client.get(
        f"/products/{payflow.id}",
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert get_denied.status_code == 403
