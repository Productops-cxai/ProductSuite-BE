"""
DatabaseSeeder — schema sync + seed data.

PLATFORM MAIN: platform_roles, organizations, products, users,
               navigation_sections, navigation_items
PLATFORM LINK: organization_product_entitlements, user_product_assignments
PAYFLOW MAIN:  payflow_roles, payflow_permissions, payflow_clients,
               payflow_user_memberships
PAYFLOW LINK:  payflow_role_permissions, payflow_user_client_assignments,
               payflow_user_client_permissions
"""

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import hash_password
from app.infrastructure.database.models import (
    NavigationItemModel,
    NavigationSectionModel,
    OrganizationModel,
    OrganizationProductEntitlementModel,
    PayflowAccountModel,
    PayflowClientModel,
    PayflowPermissionModel,
    PayflowRoleModel,
    PayflowRolePermissionModel,
    PayflowUserMembershipModel,
    PlatformRoleModel,
    ProductModel,
    UserModel,
    UserProductAssignmentModel,
)
from app.infrastructure.database.schema_sync import sync_schema
from app.infrastructure.database.session import SessionLocal, engine
from app.shared.enums import (
    EntitlementStatus,
    MenuContext,
    PayflowAiMode,
    PayflowBusinessDomain,
    PayflowClientStatus,
    PayflowClientType,
    PayflowConnectionStatus,
    PayflowDataSourceType,
    PayflowRoleCode,
    PayflowRoleScope,
    PlatformRole,
    ProductStatus,
    UserStatus,
)

# Lovable permission catalog (code = stable key, name = display label)
_PERMISSION_SEED: list[tuple[str, str, str, int]] = [
    ("view_client", "View Client", "client_case_access", 1),
    ("view_customer_accounts", "View Customer Accounts", "client_case_access", 2),
    ("view_collection_cases", "View Collection Cases", "client_case_access", 3),
    ("view_workflows", "View Workflows", "collection_operations", 10),
    ("view_communications", "View Communications", "collection_operations", 11),
    ("view_human_reviews", "View Human Reviews", "human_review", 20),
    ("approve_human_reviews", "Approve Human Reviews", "human_review", 21),
    ("modify_guide_ai_recommendation", "Modify / Guide AI Recommendation", "human_review", 22),
    ("view_rules", "View Rules", "governance", 30),
    ("create_edit_client_rules", "Create / Edit Client Rules", "governance", 31),
    ("modify_governance", "Modify Governance", "governance", 32),
    ("view_analytics", "View Analytics", "analytics", 40),
    ("modify_ai_mode", "Modify AI Mode", "client_configuration", 50),
    ("modify_client_configuration", "Modify Client Configuration", "client_configuration", 51),
]

_SUPERVISOR_PERMISSION_CODES = [
    "view_client",
    "view_customer_accounts",
    "view_collection_cases",
    "view_workflows",
    "view_communications",
    "view_human_reviews",
    "approve_human_reviews",
    "view_rules",
    "view_analytics",
]

_PAYFLOW_NAV: list[tuple[str, str, int, list[tuple[str, str, str, str, int, bool, str | None]]]] = [
    # section_key, label, sort, items: key, label, route, icon, sort, soon, required_role_code
    (
        "overview",
        "OVERVIEW",
        1,
        [("dashboard", "Dashboard", "/payflow", "overview", 1, False, None)],
    ),
    (
        "operations",
        "OPERATIONS",
        2,
        [
            ("clients", "Clients", "/payflow/clients", "people", 1, False, None),
            ("cases", "Accounts / Cases", "/payflow/cases", "products", 2, False, None),
            ("review", "Human Review", "/payflow/review", "access", 3, True, None),
        ],
    ),
    (
        "ai_operations",
        "AI OPERATIONS",
        3,
        [
            (
                "workflows",
                "Strategies / Workflows",
                "/payflow/workflows",
                "overview",
                1,
                True,
                None,
            ),
            ("comms", "Communications", "/payflow/comms", "mail", 2, True, None),
        ],
    ),
    (
        "governance",
        "GOVERNANCE",
        4,
        [("rules", "Rules", "/payflow/rules", "billing", 1, True, None)],
    ),
    (
        "administration",
        "ADMINISTRATION",
        5,
        [
            (
                "users",
                "Users & Permissions",
                "/payflow/users",
                "people",
                1,
                False,
                PayflowRoleCode.OPERATIONS_ADMIN.value,
            ),
            (
                "integrations",
                "Integrations",
                "/payflow/integrations",
                "products",
                2,
                False,
                PayflowRoleCode.OPERATIONS_ADMIN.value,
            ),
        ],
    ),
]


class DatabaseSeeder:
    """Idempotent + additive only."""

    def run(self) -> None:
        sync_schema(engine)
        db = SessionLocal()
        try:
            self._seed_platform_roles(db)
            self._seed_organizations(db)
            self._seed_products(db)
            self._seed_platform_menus(db)
            self._seed_super_admin(db)
            self._seed_demo_entitlements(db)
            self._seed_payflow_permissions(db)
            self._seed_payflow_roles(db)
            self._seed_payflow_clients(db)
            self._seed_payflow_demo_accounts(db)
            self._seed_payflow_menus(db)
            self._backfill_payflow_ops_admin_memberships(db)
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _seed_platform_roles(self, db: Session) -> None:
        roles = [
            (
                PlatformRole.PLATFORM_SUPER_ADMIN.value,
                "Platform Super Admin",
                "Full platform administration",
            ),
            (PlatformRole.PLATFORM_USER.value, "Platform User", "Standard platform user"),
        ]
        for code, name, description in roles:
            if not db.query(PlatformRoleModel).filter(PlatformRoleModel.code == code).first():
                db.add(PlatformRoleModel(code=code, name=name, description=description))
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
                db.add(
                    ProductModel(name=name, code=code, description=description, status=status)
                )
        db.flush()

    def _seed_platform_menus(self, db: Session) -> None:
        platform = (
            db.query(NavigationSectionModel)
            .filter(NavigationSectionModel.key == "platform")
            .first()
        )
        if not platform:
            platform = NavigationSectionModel(
                key="platform",
                label="PLATFORM",
                context=MenuContext.PLATFORM_ADMIN.value,
                product_code="",
                sort_order=1,
            )
            db.add(platform)
            db.flush()
        else:
            platform.product_code = platform.product_code or ""

        future = (
            db.query(NavigationSectionModel)
            .filter(NavigationSectionModel.key == "future")
            .first()
        )
        if not future:
            future = NavigationSectionModel(
                key="future",
                label="FUTURE",
                context=MenuContext.PLATFORM_ADMIN.value,
                product_code="",
                sort_order=2,
            )
            db.add(future)
            db.flush()
        else:
            future.product_code = future.product_code or ""

        platform_items = [
            ("overview", "Overview", "/platform/overview", "layout-dashboard", 1, False),
            ("products", "Products", "/platform/products", "box", 2, False),
            ("product_access", "Product Access", "/platform/product-access", "key", 3, False),
            ("people", "People", "/platform/people", "users", 4, False),
            ("email_logs", "Email Logs", "/platform/email-logs", "mail", 5, False),
        ]
        for key, label, route, icon, sort_order, soon in platform_items:
            exists = (
                db.query(NavigationItemModel)
                .filter(
                    NavigationItemModel.section_id == platform.id,
                    NavigationItemModel.key == key,
                )
                .first()
            )
            if not exists:
                db.add(
                    NavigationItemModel(
                        section_id=platform.id,
                        key=key,
                        label=label,
                        route=route,
                        icon=icon,
                        sort_order=sort_order,
                        required_role_code=PlatformRole.PLATFORM_SUPER_ADMIN.value,
                        is_coming_soon=soon,
                        is_active=True,
                    )
                )

        billing = (
            db.query(NavigationItemModel)
            .filter(
                NavigationItemModel.section_id == future.id,
                NavigationItemModel.key == "billing",
            )
            .first()
        )
        if not billing:
            db.add(
                NavigationItemModel(
                    section_id=future.id,
                    key="billing",
                    label="Billing & Invoices",
                    route="/platform/billing",
                    icon="receipt",
                    sort_order=1,
                    required_role_code=PlatformRole.PLATFORM_SUPER_ADMIN.value,
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
            db.query(PlatformRoleModel)
            .filter(PlatformRoleModel.code == PlatformRole.PLATFORM_SUPER_ADMIN.value)
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

        if created:
            products = (
                db.query(ProductModel)
                .filter(ProductModel.status == ProductStatus.ACTIVE.value)
                .all()
            )
            for product in products:
                db.add(UserProductAssignmentModel(user_id=admin.id, product_id=product.id))
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
                db.query(OrganizationProductEntitlementModel)
                .filter(
                    OrganizationProductEntitlementModel.organization_id == org.id,
                    OrganizationProductEntitlementModel.product_id == product_id,
                )
                .first()
            )
            if not row:
                db.add(
                    OrganizationProductEntitlementModel(
                        organization_id=org.id,
                        product_id=product_id,
                        status=status,
                        granted_at=now if status == EntitlementStatus.GRANTED.value else None,
                        revoked_at=now if status == EntitlementStatus.REVOKED.value else None,
                    )
                )
        db.flush()

    def _seed_payflow_permissions(self, db: Session) -> None:
        for code, name, group_key, sort_order in _PERMISSION_SEED:
            if not db.query(PayflowPermissionModel).filter(PayflowPermissionModel.code == code).first():
                db.add(
                    PayflowPermissionModel(
                        code=code,
                        name=name,
                        group_key=group_key,
                        sort_order=sort_order,
                    )
                )
        db.flush()

    def _seed_payflow_roles(self, db: Session) -> None:
        perms = {p.code: p for p in db.query(PayflowPermissionModel).all()}

        admin = (
            db.query(PayflowRoleModel)
            .filter(PayflowRoleModel.code == PayflowRoleCode.OPERATIONS_ADMIN.value)
            .first()
        )
        if not admin:
            admin = PayflowRoleModel(
                code=PayflowRoleCode.OPERATIONS_ADMIN.value,
                name="Operations Admin",
                scope=PayflowRoleScope.PLATFORM_WIDE.value,
                description=(
                    "Full operational access across every client. No client assignment required."
                ),
                is_built_in=True,
            )
            db.add(admin)
            db.flush()

        supervisor = (
            db.query(PayflowRoleModel)
            .filter(PayflowRoleModel.code == PayflowRoleCode.SUPERVISOR.value)
            .first()
        )
        if not supervisor:
            supervisor = PayflowRoleModel(
                code=PayflowRoleCode.SUPERVISOR.value,
                name="Supervisor",
                scope=PayflowRoleScope.CLIENT_SCOPED.value,
                description="Works only inside assigned clients, with per-client permissions.",
                is_built_in=True,
            )
            db.add(supervisor)
            db.flush()

        def ensure_role_perms(role: PayflowRoleModel, codes: list[str]) -> None:
            existing = {
                link.permission_id
                for link in db.query(PayflowRolePermissionModel)
                .filter(PayflowRolePermissionModel.payflow_role_id == role.id)
                .all()
            }
            for code in codes:
                perm = perms.get(code)
                if not perm or perm.id in existing:
                    continue
                db.add(
                    PayflowRolePermissionModel(
                        payflow_role_id=role.id, permission_id=perm.id
                    )
                )

        ensure_role_perms(admin, list(perms.keys()))
        ensure_role_perms(supervisor, _SUPERVISOR_PERMISSION_CODES)
        db.flush()

    def _seed_payflow_clients(self, db: Session) -> None:
        """Seed Lovable-shaped demo clients so Accounts / Integrations have data."""
        demo_clients = [
            {
                "code": "paypal",
                "name": "PayPal",
                "category": "Payments",
                "crm_system_name": "CRM",
                "channel_email": True,
                "channel_sms": True,
            },
            {
                "code": "canadian-tire",
                "name": "Canadian Tire",
                "category": "Retail",
                "crm_system_name": "CRM",
                "channel_email": True,
                "channel_sms": True,
            },
            {
                "code": "northstar-utilities",
                "name": "Northstar Utilities",
                "category": "Utilities",
                "crm_system_name": "CRM",
                "channel_email": True,
                "channel_sms": True,
            },
        ]
        for item in demo_clients:
            exists = (
                db.query(PayflowClientModel)
                .filter(PayflowClientModel.code == item["code"])
                .first()
            )
            if exists:
                # Ensure channel / connection flags for integrations demo.
                if exists.connection_status is None:
                    exists.connection_status = PayflowConnectionStatus.CONNECTED.value
                if exists.data_source_type is None:
                    exists.data_source_type = PayflowDataSourceType.CRM.value
                if exists.channel_email is None:
                    exists.channel_email = item["channel_email"]
                if exists.channel_sms is None:
                    exists.channel_sms = item["channel_sms"]
                continue
            client = PayflowClientModel(
                code=item["code"],
                name=item["name"],
                category=item["category"],
                status=PayflowClientStatus.ACTIVE.value,
                client_type=PayflowClientType.THIRD_PARTY.value,
                business_domain=PayflowBusinessDomain.COLLECTIONS.value,
                ai_mode=PayflowAiMode.SUPERVISED_AI.value,
                data_source_type=PayflowDataSourceType.CRM.value,
                connection_status=PayflowConnectionStatus.CONNECTED.value,
                crm_system_name=item["crm_system_name"],
                environment="Production",
                sync_frequency="Every 15 minutes",
                channel_email=item["channel_email"],
                channel_sms=item["channel_sms"],
                channel_whatsapp=False,
            )
            db.add(client)
            db.flush()
            from app.modules.payflow.services.client_service import PayflowClientService

            PayflowClientService(db)._seed_default_mappings(client)
        db.flush()

    def _seed_payflow_demo_accounts(self, db: Session) -> None:
        """Idempotent seed of Lovable sample customer accounts / cases."""
        clients = {c.code: c for c in db.query(PayflowClientModel).all()}
        demo_accounts = [
            {
                "client_code": "paypal",
                "customer_name": "John Smith",
                "account_reference": "PP-10482",
                "case_reference": "CASE-PP-10482-01",
                "original_balance": 5400,
                "outstanding_balance": 4250,
                "recovered_balance": 1150,
                "collection_status": "Active",
                "current_workflow": "Early Stage Collection",
                "last_action": "Email reminder sent",
                "next_action": "Reassess in 48 hours",
                "human_review": False,
                "timeline": [
                    {"label": "Account became overdue", "detail": "31 days past due", "at": "Aug 12"},
                    {"label": "Customer assessed", "detail": "Low risk, email preferred", "at": "Aug 13"},
                    {"label": "Email reminder sent", "detail": "Early stage template", "at": "Aug 14"},
                    {"label": "Partial payment received", "detail": "$1,150", "at": "Aug 15"},
                    {"label": "Next reassessment scheduled", "detail": "In 48 hours", "at": "Aug 16"},
                ],
            },
            {
                "client_code": "paypal",
                "customer_name": "Sarah Khan",
                "account_reference": "PP-11021",
                "case_reference": "CASE-PP-11021-01",
                "original_balance": 9600,
                "outstanding_balance": 8900,
                "recovered_balance": 700,
                "collection_status": "Promise to Pay",
                "current_workflow": "Promise-to-Pay Follow-Up",
                "last_action": "SMS sent",
                "next_action": "Review after promise date",
                "human_review": False,
                "timeline": [
                    {"label": "Account became overdue", "detail": "48 days past due", "at": "Jul 30"},
                    {"label": "Promise to pay captured", "detail": "$8,900 by Aug 28", "at": "Aug 05"},
                    {"label": "SMS sent", "detail": "Promise reminder", "at": "Aug 20"},
                ],
            },
            {
                "client_code": "paypal",
                "customer_name": "Michael Brown",
                "account_reference": "PP-12098",
                "case_reference": "CASE-PP-12098-01",
                "original_balance": 3600,
                "outstanding_balance": 2100,
                "recovered_balance": 1500,
                "collection_status": "Payment Plan",
                "current_workflow": "Payment Plan Monitoring",
                "last_action": "Installment received",
                "next_action": "Next installment in 7 days",
                "human_review": False,
                "timeline": [
                    {"label": "Payment plan agreed", "detail": "6 monthly installments", "at": "Jul 25"},
                    {"label": "Installment received", "detail": "$500", "at": "Aug 18"},
                ],
            },
            {
                "client_code": "paypal",
                "customer_name": "David Lee",
                "account_reference": "PP-88831",
                "case_reference": "CASE-PP-88831-01",
                "original_balance": 12500,
                "outstanding_balance": 12500,
                "recovered_balance": 0,
                "collection_status": "Human Review",
                "current_workflow": "Escalated Collection",
                "last_action": "AI recommendation created",
                "next_action": "Awaiting supervisor",
                "human_review": True,
                "timeline": [
                    {"label": "Escalation triggered", "detail": "No response after 4 attempts", "at": "Aug 10"},
                    {"label": "AI recommendation created", "detail": "Settlement offer proposed", "at": "Aug 19"},
                ],
            },
            {
                "client_code": "canadian-tire",
                "customer_name": "Emily Jones",
                "account_reference": "CT-20394",
                "case_reference": "CASE-CT-20394-01",
                "original_balance": 7800,
                "outstanding_balance": 7300,
                "recovered_balance": 500,
                "collection_status": "Active",
                "current_workflow": "Progressive Reminder",
                "last_action": "SMS sent",
                "next_action": "Reassess tomorrow",
                "human_review": False,
                "timeline": [
                    {"label": "SMS sent", "detail": "Reminder 2 of 4", "at": "Aug 19"},
                ],
            },
            {
                "client_code": "canadian-tire",
                "customer_name": "Robert Chen",
                "account_reference": "CT-21877",
                "case_reference": "CASE-CT-21877-01",
                "original_balance": 4100,
                "outstanding_balance": 1900,
                "recovered_balance": 2200,
                "collection_status": "Payment Plan",
                "current_workflow": "Payment Plan Monitoring",
                "last_action": "Installment received",
                "next_action": "Next installment in 12 days",
                "human_review": False,
                "timeline": [
                    {"label": "Installment received", "detail": "$1,100", "at": "Aug 14"},
                ],
            },
            {
                "client_code": "canadian-tire",
                "customer_name": "Priya Nair",
                "account_reference": "CT-22540",
                "case_reference": "CASE-CT-22540-01",
                "original_balance": 3200,
                "outstanding_balance": 3200,
                "recovered_balance": 0,
                "collection_status": "Human Review",
                "current_workflow": "Escalated Collection",
                "last_action": "Dispute flagged by customer",
                "next_action": "Awaiting supervisor",
                "human_review": True,
                "timeline": [
                    {"label": "Dispute flagged by customer", "detail": "Charge disputed via reply", "at": "Aug 18"},
                ],
            },
            {
                "client_code": "northstar-utilities",
                "customer_name": "Laura Fitzgerald",
                "account_reference": "NS-30112",
                "case_reference": "CASE-NS-30112-01",
                "original_balance": 2600,
                "outstanding_balance": 2450,
                "recovered_balance": 150,
                "collection_status": "Active",
                "current_workflow": "Early Stage Collection",
                "last_action": "Email reminder sent",
                "next_action": "Reassess in 72 hours",
                "human_review": False,
                "timeline": [
                    {"label": "Email reminder sent", "detail": "Soft reminder", "at": "Aug 16"},
                ],
            },
            {
                "client_code": "northstar-utilities",
                "customer_name": "Marcus Webb",
                "account_reference": "NS-31450",
                "case_reference": "CASE-NS-31450-01",
                "original_balance": 5900,
                "outstanding_balance": 5400,
                "recovered_balance": 500,
                "collection_status": "Promise to Pay",
                "current_workflow": "Promise-to-Pay Follow-Up",
                "last_action": "Promise captured on call",
                "next_action": "Review after promise date",
                "human_review": False,
                "timeline": [
                    {"label": "Promise to pay captured", "detail": "$5,400 by Sep 01", "at": "Aug 12"},
                ],
            },
        ]

        for item in demo_accounts:
            client = clients.get(item["client_code"])
            if not client:
                continue
            exists = (
                db.query(PayflowAccountModel)
                .filter(
                    PayflowAccountModel.client_id == client.id,
                    PayflowAccountModel.account_reference == item["account_reference"],
                )
                .first()
            )
            if exists:
                continue
            db.add(
                PayflowAccountModel(
                    client_id=client.id,
                    customer_name=item["customer_name"],
                    account_reference=item["account_reference"],
                    case_reference=item["case_reference"],
                    original_balance=item["original_balance"],
                    outstanding_balance=item["outstanding_balance"],
                    recovered_balance=item["recovered_balance"],
                    collection_status=item["collection_status"],
                    current_workflow=item["current_workflow"],
                    last_action=item["last_action"],
                    next_action=item["next_action"],
                    human_review=item["human_review"],
                    timeline=item["timeline"],
                )
            )
        db.flush()

    def _seed_payflow_menus(self, db: Session) -> None:
        unlock_keys = {"clients", "cases", "integrations"}
        for section_key, label, sort_order, items in _PAYFLOW_NAV:
            section = (
                db.query(NavigationSectionModel)
                .filter(
                    NavigationSectionModel.key == section_key,
                    NavigationSectionModel.product_code == "PAYFLOW",
                )
                .first()
            )
            if not section:
                section = NavigationSectionModel(
                    key=section_key,
                    label=label,
                    context=MenuContext.PRODUCT.value,
                    product_code="PAYFLOW",
                    sort_order=sort_order,
                )
                db.add(section)
                db.flush()

            for key, item_label, route, icon, item_sort, soon, role_code in items:
                exists = (
                    db.query(NavigationItemModel)
                    .filter(
                        NavigationItemModel.section_id == section.id,
                        NavigationItemModel.key == key,
                    )
                    .first()
                )
                if not exists:
                    db.add(
                        NavigationItemModel(
                            section_id=section.id,
                            key=key,
                            label=item_label,
                            route=route,
                            icon=icon,
                            sort_order=item_sort,
                            required_role_code=role_code,
                            is_coming_soon=soon,
                            is_active=True,
                        )
                    )
                elif key in unlock_keys and exists.is_coming_soon:
                    exists.is_coming_soon = False
        db.flush()

    def _backfill_payflow_ops_admin_memberships(self, db: Session) -> None:
        """Existing PAYFLOW assignees without membership become Operations Admin (2A)."""
        payflow = db.query(ProductModel).filter(ProductModel.code == "PAYFLOW").first()
        ops_role = (
            db.query(PayflowRoleModel)
            .filter(PayflowRoleModel.code == PayflowRoleCode.OPERATIONS_ADMIN.value)
            .first()
        )
        if not payflow or not ops_role:
            return

        assigned_user_ids = {
            row.user_id
            for row in db.query(UserProductAssignmentModel)
            .filter(UserProductAssignmentModel.product_id == payflow.id)
            .all()
        }
        existing_memberships = {
            row.user_id for row in db.query(PayflowUserMembershipModel).all()
        }
        for user_id in assigned_user_ids - existing_memberships:
            db.add(
                PayflowUserMembershipModel(
                    user_id=user_id,
                    payflow_role_id=ops_role.id,
                )
            )
        db.flush()


def run_seeder() -> None:
    DatabaseSeeder().run()
