"""
DatabaseSeeder — single place for schema sync + seed data.

MAIN: roles, organizations, products, users, menu_sections, menu_items
LINK: organization_products, user_products
"""

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.core.config import settings
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
from app.infrastructure.database.schema_sync import sync_schema
from app.infrastructure.database.session import SessionLocal, engine
from app.shared.enums import (
    EntitlementStatus,
    MenuContext,
    PlatformRole,
    ProductStatus,
    UserStatus,
)


class DatabaseSeeder:
    """Idempotent + additive only.

    On every app start:
    - creates missing tables / nullable columns
    - inserts missing default rows (roles, orgs, products, menus, demo grants)
    Never updates or deletes existing people, product assignments, org grants,
    or product fields — so restart after code changes does not undo manual work.
    """

    def run(self) -> None:
        sync_schema(engine)
        db = SessionLocal()
        try:
            self._seed_roles(db)
            self._seed_organizations(db)
            self._seed_products(db)
            self._seed_menus(db)
            self._seed_super_admin(db)
            self._seed_demo_entitlements(db)
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _seed_roles(self, db: Session) -> None:
        roles = [
            (PlatformRole.PLATFORM_SUPER_ADMIN.value, "Platform Super Admin", "Full platform administration"),
            (PlatformRole.PLATFORM_USER.value, "Platform User", "Standard platform user"),
        ]
        for code, name, description in roles:
            if not db.query(RoleModel).filter(RoleModel.code == code).first():
                db.add(RoleModel(code=code, name=name, description=description))
        db.flush()

    def _seed_organizations(self, db: Session) -> None:
        defaults = [
            ("PayFlow Operations (Internal)", True),
            ("Northstar Financial Group", False),
            ("Arcadia Utilities", False),
        ]
        for name, is_internal in defaults:
            if not db.query(OrganizationModel).filter(OrganizationModel.name == name).first():
                db.add(OrganizationModel(name=name, is_internal=is_internal))
        db.flush()

    def _seed_products(self, db: Session) -> None:
        products = [
            (
                "PayFlow",
                "PAYFLOW",
                "Collections operations: client portfolios, customer accounts, "
                "collection cases, adaptive workflows and governed AI decisions.",
                ProductStatus.ACTIVE.value,
            ),
            (
                "InsightIQ",
                "INSIGHTIQ",
                "Sample second product, registered to show multi-product access. "
                "It has no operational screens in this phase.",
                ProductStatus.ACTIVE.value,
            ),
        ]
        for name, code, description, status in products:
            if not db.query(ProductModel).filter(ProductModel.code == code).first():
                db.add(ProductModel(name=name, code=code, description=description, status=status))
        db.flush()

    def _seed_menus(self, db: Session) -> None:
        platform = db.query(MenuSectionModel).filter(MenuSectionModel.key == "platform").first()
        if not platform:
            platform = MenuSectionModel(
                key="platform",
                label="PLATFORM",
                context=MenuContext.PLATFORM_ADMIN.value,
                sort_order=1,
            )
            db.add(platform)
            db.flush()

        future = db.query(MenuSectionModel).filter(MenuSectionModel.key == "future").first()
        if not future:
            future = MenuSectionModel(
                key="future",
                label="FUTURE",
                context=MenuContext.PLATFORM_ADMIN.value,
                sort_order=2,
            )
            db.add(future)
            db.flush()

        platform_items = [
            ("overview", "Overview", "/platform/overview", "layout-dashboard", 1, False),
            ("products", "Products", "/platform/products", "box", 2, False),
            ("product_access", "Product Access", "/platform/product-access", "key", 3, False),
            ("people", "People", "/platform/people", "users", 4, False),
            ("email_logs", "Email Logs", "/platform/email-logs", "mail", 5, False),
        ]
        for key, label, route, icon, sort_order, soon in platform_items:
            exists = (
                db.query(MenuItemModel)
                .filter(MenuItemModel.section_id == platform.id, MenuItemModel.key == key)
                .first()
            )
            if not exists:
                db.add(
                    MenuItemModel(
                        section_id=platform.id,
                        key=key,
                        label=label,
                        route=route,
                        icon=icon,
                        sort_order=sort_order,
                        required_role=PlatformRole.PLATFORM_SUPER_ADMIN.value,
                        is_coming_soon=soon,
                        is_active=True,
                    )
                )

        billing = (
            db.query(MenuItemModel)
            .filter(MenuItemModel.section_id == future.id, MenuItemModel.key == "billing")
            .first()
        )
        if not billing:
            db.add(
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
        db.flush()

    def _seed_super_admin(self, db: Session) -> None:
        org = (
            db.query(OrganizationModel)
            .filter(OrganizationModel.name == "PayFlow Operations (Internal)")
            .first()
        )
        admin_role = (
            db.query(RoleModel)
            .filter(RoleModel.code == PlatformRole.PLATFORM_SUPER_ADMIN.value)
            .first()
        )
        if not org or not admin_role:
            return

        admin = (
            db.query(UserModel)
            .filter(UserModel.email == settings.SEED_SUPER_ADMIN_EMAIL.lower())
            .first()
        )
        created = False
        if not admin:
            admin = UserModel(
                full_name=settings.SEED_SUPER_ADMIN_NAME,
                email=settings.SEED_SUPER_ADMIN_EMAIL.lower(),
                password_hash=hash_password(settings.SEED_SUPER_ADMIN_PASSWORD),
                organization_id=org.id,
                role_id=admin_role.id,
                status=UserStatus.ACTIVE.value,
            )
            db.add(admin)
            db.flush()
            created = True

        # Only seed product assignments for a brand-new admin so manual
        # assign/remove on People page is not overwritten on every restart.
        if created:
            products = (
                db.query(ProductModel)
                .filter(ProductModel.status == ProductStatus.ACTIVE.value)
                .all()
            )
            for product in products:
                db.add(UserProductModel(user_id=admin.id, product_id=product.id))
        db.flush()

    def _seed_demo_entitlements(self, db: Session) -> None:
        payflow = db.query(ProductModel).filter(ProductModel.code == "PAYFLOW").first()
        insight = db.query(ProductModel).filter(ProductModel.code == "INSIGHTIQ").first()
        orgs = {o.name: o for o in db.query(OrganizationModel).all()}
        if not payflow or not insight:
            return

        now = datetime.now(timezone.utc)
        grants = [
            ("PayFlow Operations (Internal)", payflow.id, EntitlementStatus.GRANTED.value),
            ("PayFlow Operations (Internal)", insight.id, EntitlementStatus.GRANTED.value),
            ("Northstar Financial Group", payflow.id, EntitlementStatus.GRANTED.value),
            ("Northstar Financial Group", insight.id, EntitlementStatus.REVOKED.value),
            ("Arcadia Utilities", payflow.id, EntitlementStatus.REVOKED.value),
            ("Arcadia Utilities", insight.id, EntitlementStatus.REVOKED.value),
        ]
        for org_name, product_id, status in grants:
            org = orgs.get(org_name)
            if not org:
                continue
            row = (
                db.query(OrganizationProductModel)
                .filter(
                    OrganizationProductModel.organization_id == org.id,
                    OrganizationProductModel.product_id == product_id,
                )
                .first()
            )
            if not row:
                db.add(
                    OrganizationProductModel(
                        organization_id=org.id,
                        product_id=product_id,
                        status=status,
                        granted_at=now if status == EntitlementStatus.GRANTED.value else None,
                        revoked_at=now if status == EntitlementStatus.REVOKED.value else None,
                    )
                )
        db.flush()


def run_seeder() -> None:
    DatabaseSeeder().run()
