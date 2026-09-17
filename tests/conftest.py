import uuid
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.security import hash_password
from app.infrastructure.database.models import (
    MenuItemModel,
    MenuSectionModel,
    OrganizationModel,
    OrganizationProductModel,
    ProductModel,
    RoleModel,
    UserModel,
    UserProductModel,
)
from app.infrastructure.database.session import Base, get_db
from app.main import app
from app.shared.enums import (
    EntitlementStatus,
    MenuContext,
    PersonStatus,
    PlatformRole,
    ProductStatus,
)


@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = TestingSessionLocal()

    admin_role = RoleModel(
        code=PlatformRole.PLATFORM_SUPER_ADMIN.value,
        name="Platform Super Admin",
    )
    user_role = RoleModel(
        code=PlatformRole.PLATFORM_USER.value,
        name="Platform User",
    )
    session.add_all([admin_role, user_role])
    session.flush()

    org = OrganizationModel(name="Test Org", is_internal=True)
    session.add(org)
    session.flush()

    payflow = ProductModel(
        name="PayFlow",
        code="PAYFLOW",
        description="Collections",
        status=ProductStatus.ACTIVE.value,
    )
    insight = ProductModel(
        name="InsightIQ",
        code="INSIGHTIQ",
        description="Analytics sample",
        status=ProductStatus.ACTIVE.value,
    )
    session.add_all([payflow, insight])
    session.flush()

    admin = UserModel(
        full_name="Platform Super Admin",
        email="admin@payflow.ai",
        password_hash=hash_password("Admin@12345"),
        organization_id=org.id,
        role_id=admin_role.id,
        status=PersonStatus.ACTIVE.value,
    )
    user = UserModel(
        full_name="Ops User",
        email="ops@example.com",
        password_hash=hash_password("User@12345"),
        organization_id=org.id,
        role_id=user_role.id,
        status=PersonStatus.ACTIVE.value,
    )
    session.add_all([admin, user])
    session.flush()

    session.add_all(
        [
            OrganizationProductModel(
                organization_id=org.id,
                product_id=payflow.id,
                status=EntitlementStatus.GRANTED.value,
                granted_at=datetime.now(timezone.utc),
            ),
            OrganizationProductModel(
                organization_id=org.id,
                product_id=insight.id,
                status=EntitlementStatus.GRANTED.value,
                granted_at=datetime.now(timezone.utc),
            ),
            UserProductModel(user_id=admin.id, product_id=payflow.id),
            UserProductModel(user_id=admin.id, product_id=insight.id),
            UserProductModel(user_id=user.id, product_id=payflow.id),
        ]
    )

    section = MenuSectionModel(
        key="platform",
        label="PLATFORM",
        context=MenuContext.PLATFORM_ADMIN.value,
        sort_order=1,
    )
    session.add(section)
    session.flush()
    session.add(
        MenuItemModel(
            section_id=section.id,
            key="overview",
            label="Overview",
            route="/platform/overview",
            icon="layout",
            sort_order=1,
            required_role=PlatformRole.PLATFORM_SUPER_ADMIN.value,
            is_coming_soon=False,
            is_active=True,
        )
    )
    future = MenuSectionModel(
        key="future",
        label="FUTURE",
        context=MenuContext.PLATFORM_ADMIN.value,
        sort_order=2,
    )
    session.add(future)
    session.flush()
    session.add(
        MenuItemModel(
            section_id=future.id,
            key="billing",
            label="Billing & Invoices",
            route="/platform/billing",
            icon="receipt",
            sort_order=1,
            required_role=PlatformRole.PLATFORM_SUPER_ADMIN.value,
            is_coming_soon=True,
            is_active=True,
        )
    )
    session.commit()

    yield session
    session.close()


@pytest.fixture()
def client(db_session):
    def _override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = _override_get_db
    with patch("app.main.run_seeder"):
        with TestClient(app) as test_client:
            yield test_client
    app.dependency_overrides.clear()


@pytest.fixture()
def admin_token(client):
    response = client.post(
        "/auth/login",
        json={"email": "admin@payflow.ai", "password": "Admin@12345"},
    )
    assert response.status_code == 200
    return response.json()["access_token"]


@pytest.fixture()
def user_token(client):
    response = client.post(
        "/auth/login",
        json={"email": "ops@example.com", "password": "User@12345"},
    )
    assert response.status_code == 200
    return response.json()["access_token"]
