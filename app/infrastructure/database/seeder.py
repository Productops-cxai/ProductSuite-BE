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
    PayflowCommunicationModel,
    PayflowHumanReviewModel,
    PayflowPermissionModel,
    PayflowRoleModel,
    PayflowRolePermissionModel,
    PayflowRuleModel,
    PayflowStrategyModel,
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
            ("review", "Human Review", "/payflow/review", "access", 3, False, None),
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
                False,
                None,
            ),
            ("comms", "Communications", "/payflow/comms", "mail", 2, False, None),
        ],
    ),
    (
        "governance",
        "GOVERNANCE",
        4,
        [("rules", "Rules", "/payflow/rules", "billing", 1, False, None)],
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
            self._seed_payflow_demo_rules(db)
            self._seed_payflow_demo_reviews(db)
            self._seed_payflow_demo_strategies(db)
            self._seed_payflow_demo_communications(db)
            self._seed_payflow_menus(db)
            self._backfill_payflow_ops_admin_memberships(db)
            self._seed_payflow_demo_notifications(db)
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

    def _seed_payflow_demo_rules(self, db: Session) -> None:
        clients = {c.code: c for c in db.query(PayflowClientModel).all()}
        demo_rules = [
            {
                "code": "high-balance-review",
                "name": "High Balance Review",
                "description": "Routes high-value accounts to a supervisor before any autonomous collection action is executed.",
                "rule_type": "Client Rule",
                "client_code": "paypal",
                "category": "Amount",
                "logic": "ALL",
                "conditions": [
                    {
                        "id": "hb-1",
                        "field": "Outstanding Balance",
                        "operator": "Greater Than",
                        "value": "10000",
                    }
                ],
                "action": "Require Human Review",
                "status": "Active",
                "created_by": "Daniya Shaikh",
                "triggers_7d": 14,
                "applied_to": ["paypal"],
                "history": [
                    {"at": "12 Sep 2026", "change": "Threshold updated", "by": "Daniya Shaikh"},
                    {"at": "10 Sep 2026", "change": "Rule activated", "by": "Daniya Shaikh"},
                ],
            },
            {
                "code": "repeated-attempts-escalation",
                "name": "Repeated Attempts Escalation",
                "description": "Escalates accounts where repeated outreach has not produced a response or payment commitment.",
                "rule_type": "System Rule",
                "client_code": None,
                "category": "Collection Attempts",
                "logic": "ALL",
                "conditions": [
                    {
                        "id": "ra-1",
                        "field": "Unsuccessful Attempts",
                        "operator": "Greater Than or Equal To",
                        "value": "3",
                    }
                ],
                "action": "Require Human Review",
                "status": "Active",
                "created_by": "Daniya Shaikh",
                "triggers_7d": 42,
                "applied_to": ["canadian-tire", "northstar-utilities"],
                "history": [
                    {"at": "09 Sep 2026", "change": "Applied to Canadian Tire", "by": "Daniya Shaikh"},
                    {"at": "02 Sep 2026", "change": "Rule activated", "by": "Daniya Shaikh"},
                ],
            },
            {
                "code": "low-confidence-review",
                "name": "Low Confidence Review",
                "description": "Requires supervisor judgement when the AI recommendation confidence falls below the configured threshold.",
                "rule_type": "Client Rule",
                "client_code": "canadian-tire",
                "category": "AI Confidence",
                "logic": "ALL",
                "conditions": [
                    {"id": "lc-1", "field": "AI Confidence", "operator": "Less Than", "value": "70"}
                ],
                "action": "Require Human Review",
                "status": "Active",
                "created_by": "Zeeshan",
                "triggers_7d": 9,
                "applied_to": ["canadian-tire"],
                "history": [
                    {"at": "11 Sep 2026", "change": "Threshold updated", "by": "Zeeshan"},
                    {"at": "04 Sep 2026", "change": "Rule created", "by": "Zeeshan"},
                ],
            },
            {
                "code": "dispute-detected-review",
                "name": "Dispute Detected Review",
                "description": "Holds collection activity when a customer disputes the balance.",
                "rule_type": "System Rule",
                "client_code": None,
                "category": "Customer Risk",
                "logic": "ANY",
                "conditions": [
                    {"id": "dd-1", "field": "Dispute Flag", "operator": "Is", "value": "Yes"},
                    {
                        "id": "dd-2",
                        "field": "Customer Reply Content",
                        "operator": "Contains",
                        "value": "dispute",
                    },
                ],
                "action": "Hold Action",
                "status": "Active",
                "created_by": "Daniya Shaikh",
                "triggers_7d": 6,
                "applied_to": ["paypal", "canadian-tire"],
                "history": [{"at": "07 Sep 2026", "change": "Rule activated", "by": "Daniya Shaikh"}],
            },
            {
                "code": "broken-promise-escalation",
                "name": "Broken Promise Escalation",
                "description": "Escalates cases where a promise-to-pay was not honoured.",
                "rule_type": "Client Rule",
                "client_code": "canadian-tire",
                "category": "Promise-to-Pay",
                "logic": "ALL",
                "conditions": [
                    {"id": "bp-1", "field": "Promise-to-Pay Status", "operator": "Is", "value": "Broken"},
                    {
                        "id": "bp-2",
                        "field": "Outstanding Balance",
                        "operator": "Greater Than",
                        "value": "2500",
                    },
                ],
                "action": "Escalate Case",
                "status": "Draft",
                "created_by": "Zeeshan",
                "triggers_7d": 0,
                "applied_to": ["canadian-tire"],
                "history": [{"at": "12 Sep 2026", "change": "Draft created", "by": "Zeeshan"}],
            },
            {
                "code": "quiet-period-guard",
                "name": "Quiet Period Guard",
                "description": "Prevents further outreach when the account has already received the configured weekly message volume.",
                "rule_type": "System Rule",
                "client_code": None,
                "category": "Communication",
                "logic": "ALL",
                "conditions": [
                    {
                        "id": "qp-1",
                        "field": "Messages Sent (7 days)",
                        "operator": "Greater Than or Equal To",
                        "value": "4",
                    }
                ],
                "action": "Prevent Communication",
                "status": "Active",
                "created_by": "Daniya Shaikh",
                "triggers_7d": 28,
                "applied_to": ["paypal", "canadian-tire", "northstar-utilities"],
                "history": [{"at": "05 Sep 2026", "change": "Rule activated", "by": "Daniya Shaikh"}],
            },
            {
                "code": "northstar-hardship-review",
                "name": "Hardship Signal Review",
                "description": "Client-specific review for utility customers signalling financial hardship.",
                "rule_type": "Client Rule",
                "client_code": "northstar-utilities",
                "category": "Customer Risk",
                "logic": "ALL",
                "conditions": [
                    {"id": "nh-1", "field": "Customer Risk Level", "operator": "Is", "value": "High"}
                ],
                "action": "Require Human Review",
                "status": "Active",
                "created_by": "Sarah",
                "triggers_7d": 5,
                "applied_to": ["northstar-utilities"],
                "history": [{"at": "10 Sep 2026", "change": "Rule activated", "by": "Sarah"}],
            },
        ]
        for item in demo_rules:
            if db.query(PayflowRuleModel).filter(PayflowRuleModel.code == item["code"]).first():
                continue
            client_id = None
            if item["client_code"]:
                client = clients.get(item["client_code"])
                if not client:
                    continue
                client_id = client.id
            db.add(
                PayflowRuleModel(
                    code=item["code"],
                    name=item["name"],
                    description=item["description"],
                    rule_type=item["rule_type"],
                    client_id=client_id,
                    category=item["category"],
                    logic=item["logic"],
                    conditions=item["conditions"],
                    action=item["action"],
                    status=item["status"],
                    created_by=item["created_by"],
                    triggers_7d=item["triggers_7d"],
                    applied_to=item["applied_to"],
                    history=item["history"],
                )
            )
        db.flush()

    def _seed_payflow_demo_reviews(self, db: Session) -> None:
        clients = {c.code: c for c in db.query(PayflowClientModel).all()}
        accounts = {
            a.account_reference: a for a in db.query(PayflowAccountModel).all()
        }
        rules = {r.code: r for r in db.query(PayflowRuleModel).all()}
        demo_reviews = [
            {
                "code": "rev-1041",
                "client_code": "paypal",
                "account_reference": "PP-10482",
                "rule_code": "repeated-attempts-escalation",
                "priority": "High",
                "reason": "Repeated unsuccessful attempts",
                "condition_text": "Unsuccessful Attempts greater than or equal to 3",
                "observed_value": "4 unsuccessful attempts",
                "proposed_action": "Move to stronger collection treatment",
                "confidence": 92,
                "explanation": [
                    "The customer has received multiple reminders without payment.",
                    "The latest communication was opened but no payment link interaction occurred.",
                    "The configured governance rule requires supervisor review before stronger treatment is applied.",
                ],
                "context": [
                    {"label": "Previous Attempts", "value": "4"},
                    {"label": "Last Communication", "value": "SMS Reminder"},
                    {"label": "Payment Status", "value": "Partial Payment Previously Received"},
                ],
                "timeline": [
                    {"at": "12 Sep · 09:42", "label": "SMS Reminder Delivered"},
                    {"at": "12 Sep · 12:01", "label": "Human Review Created"},
                ],
                "waiting_minutes": 18,
                "status": "Awaiting Review",
                "assigned_supervisor": "Zeeshan",
                "days_past_due": 42,
                "history": [
                    {
                        "at": "12 Sep · 12:01",
                        "event": "Human review created",
                        "detail": "Repeated Attempts Escalation required supervisor judgement",
                    }
                ],
            },
            {
                "code": "rev-1042",
                "client_code": "canadian-tire",
                "account_reference": "CT-20394",
                "rule_code": "low-confidence-review",
                "priority": "Medium",
                "reason": "Low decision confidence",
                "condition_text": "AI Confidence less than 70",
                "observed_value": "64% confidence",
                "proposed_action": "Change communication strategy",
                "confidence": 64,
                "explanation": [
                    "Engagement signals are mixed: messages are delivered but rarely opened.",
                    "Confidence fell below the client threshold, so a supervisor confirms the strategy change.",
                ],
                "context": [
                    {"label": "Previous Attempts", "value": "2"},
                    {"label": "Last Communication", "value": "SMS Reminder"},
                ],
                "timeline": [
                    {"at": "12 Sep · 11:16", "label": "Human Review Created"},
                ],
                "waiting_minutes": 42,
                "status": "Awaiting Review",
                "assigned_supervisor": "Zeeshan",
                "days_past_due": 27,
                "history": [
                    {
                        "at": "12 Sep · 11:16",
                        "event": "Human review created",
                        "detail": "Low Confidence Review",
                    }
                ],
            },
            {
                "code": "rev-1043",
                "client_code": "paypal",
                "account_reference": "PP-11021",
                "rule_code": "high-balance-review",
                "priority": "Normal",
                "reason": "Payment arrangement exception",
                "condition_text": "Outstanding Balance greater than $10,000",
                "observed_value": "$8,900 outstanding with an extended arrangement request",
                "proposed_action": "Modify payment treatment",
                "confidence": 78,
                "explanation": [
                    "The customer requested a longer arrangement than the configured standard.",
                    "Arrangement exceptions require supervisor confirmation before they are offered.",
                ],
                "context": [
                    {"label": "Promise-to-Pay", "value": "Active · due 28 Sep"},
                ],
                "timeline": [
                    {"at": "12 Sep · 10:05", "label": "Human Review Created"},
                ],
                "waiting_minutes": 72,
                "status": "Awaiting Review",
                "assigned_supervisor": None,
                "days_past_due": 48,
                "history": [
                    {
                        "at": "12 Sep · 10:05",
                        "event": "Human review created",
                        "detail": "High Balance Review",
                    }
                ],
            },
            {
                "code": "rev-1044",
                "client_code": "paypal",
                "account_reference": "PP-88831",
                "rule_code": "high-balance-review",
                "priority": "High",
                "reason": "High balance treatment",
                "condition_text": "Outstanding Balance greater than $10,000",
                "observed_value": "$12,500 outstanding",
                "proposed_action": "Send settlement offer",
                "confidence": 88,
                "explanation": [
                    "No payment or engagement has been recorded after four outreach attempts.",
                    "A settlement offer materially changes the outcome, so supervisor approval is required.",
                ],
                "context": [
                    {"label": "Previous Attempts", "value": "4"},
                    {"label": "Payment Status", "value": "Unpaid"},
                ],
                "timeline": [
                    {"at": "12 Sep · 06:31", "label": "Human Review Created"},
                ],
                "waiting_minutes": 330,
                "status": "Awaiting Review",
                "assigned_supervisor": "Zeeshan",
                "days_past_due": 96,
                "history": [
                    {
                        "at": "12 Sep · 06:31",
                        "event": "Human review created",
                        "detail": "High Balance Review",
                    }
                ],
            },
            {
                "code": "rev-1045",
                "client_code": "canadian-tire",
                "account_reference": "CT-22540",
                "rule_code": "dispute-detected-review",
                "priority": "High",
                "reason": "Customer dispute raised",
                "condition_text": 'Customer Reply Content contains "dispute"',
                "observed_value": "Reply flagged as a balance dispute",
                "proposed_action": "Hold collection activity pending dispute review",
                "confidence": None,
                "explanation": [
                    "The customer disputed the balance in a direct reply.",
                    "Collection activity is paused while the dispute is assessed.",
                ],
                "context": [
                    {"label": "Last Customer Engagement", "value": "Replied · dispute raised"},
                ],
                "timeline": [
                    {"at": "11 Sep · 15:04", "label": "Human Review Created"},
                    {"at": "11 Sep · 16:40", "label": "Placed On Hold by Zeeshan"},
                ],
                "waiting_minutes": 1290,
                "status": "On Hold",
                "assigned_supervisor": "Zeeshan",
                "final_action": "Collection activity paused",
                "hold_until": "14 Sep 2026",
                "days_past_due": 62,
                "history": [
                    {
                        "at": "11 Sep · 16:40",
                        "event": "Held until 14 Sep 2026",
                        "detail": "Customer contacted support and requested 48 hours",
                        "by": "Zeeshan",
                    }
                ],
            },
            {
                "code": "rev-1046",
                "client_code": "canadian-tire",
                "account_reference": "CT-21877",
                "rule_code": "broken-promise-escalation",
                "priority": "Normal",
                "reason": "Payment arrangement exception",
                "condition_text": "Missed Installments greater than or equal to 1",
                "observed_value": "1 missed installment, then paid",
                "proposed_action": "Move to stronger collection treatment",
                "confidence": 71,
                "explanation": [
                    "One installment was missed before the customer paid late.",
                    "Supervisor guidance kept the customer on the existing plan.",
                ],
                "context": [
                    {"label": "Payment Status", "value": "Partially Paid"},
                ],
                "timeline": [
                    {"at": "10 Sep · 09:12", "label": "Supervisor Modified Recommendation"},
                ],
                "waiting_minutes": 71,
                "status": "Modified",
                "assigned_supervisor": "Zeeshan",
                "final_action": "Continue current treatment with adjusted communication",
                "guidance": "Customer made a recent partial payment. Maintain softer tone for the next communication.",
                "days_past_due": 40,
                "history": [
                    {
                        "at": "10 Sep · 09:12",
                        "event": "Supervisor modified recommendation",
                        "detail": "Final action: Continue current treatment with adjusted communication",
                        "by": "Zeeshan",
                    }
                ],
            },
            {
                "code": "rev-1047",
                "client_code": "northstar-utilities",
                "account_reference": "NS-31450",
                "rule_code": "northstar-hardship-review",
                "priority": "Medium",
                "reason": "Hardship signal detected",
                "condition_text": "Customer Risk Level is High",
                "observed_value": "Hardship language detected in reply",
                "proposed_action": "Pause outreach and request updated contact details",
                "confidence": 69,
                "explanation": [
                    "The customer indicated financial hardship in a recent reply.",
                    "Utility hardship handling requires a supervisor decision before further outreach.",
                ],
                "context": [
                    {"label": "Last Customer Engagement", "value": "Replied · hardship mentioned"},
                ],
                "timeline": [
                    {"at": "12 Sep · 09:02", "label": "Human Review Created"},
                ],
                "waiting_minutes": 195,
                "status": "Awaiting Review",
                "assigned_supervisor": "Sarah",
                "days_past_due": 55,
                "history": [
                    {
                        "at": "12 Sep · 09:02",
                        "event": "Human review created",
                        "detail": "Hardship Signal Review",
                    }
                ],
            },
        ]
        for item in demo_reviews:
            if (
                db.query(PayflowHumanReviewModel)
                .filter(PayflowHumanReviewModel.code == item["code"])
                .first()
            ):
                continue
            client = clients.get(item["client_code"])
            account = accounts.get(item["account_reference"])
            rule = rules.get(item["rule_code"])
            if not client or not account:
                continue
            db.add(
                PayflowHumanReviewModel(
                    code=item["code"],
                    client_id=client.id,
                    account_id=account.id,
                    rule_id=rule.id if rule else None,
                    priority=item["priority"],
                    reason=item["reason"],
                    condition_text=item["condition_text"],
                    observed_value=item["observed_value"],
                    proposed_action=item["proposed_action"],
                    confidence=item.get("confidence"),
                    explanation=item.get("explanation"),
                    context=item.get("context"),
                    timeline=item.get("timeline"),
                    waiting_minutes=item["waiting_minutes"],
                    status=item["status"],
                    assigned_supervisor=item.get("assigned_supervisor"),
                    final_action=item.get("final_action"),
                    guidance=item.get("guidance"),
                    rejection_reason=item.get("rejection_reason"),
                    hold_until=item.get("hold_until"),
                    history=item.get("history"),
                    days_past_due=item.get("days_past_due", 0),
                )
            )
        db.flush()

    def _seed_payflow_demo_strategies(self, db: Session) -> None:
        clients = {c.code: c for c in db.query(PayflowClientModel).all()}
        demo = [
            {
                "code": "pp-early-recovery",
                "name": "Early Stage Collection",
                "client_code": "paypal",
                "status": "Active",
                "origin": "AI Proposed",
                "version": 3,
                "summary": "Soft email then SMS reminder for early-stage balances under progressive reassessment.",
                "coverage": "Early-stage · low–mid balance",
                "segment": {
                    "age_band": "25-44",
                    "balance_band": "$1K–$5K",
                    "delinquency": "1–30 days",
                },
                "steps": [
                    {"id": "t1", "kind": "Trigger", "title": "Account overdue"},
                    {
                        "id": "c1",
                        "kind": "Communication",
                        "title": "Email reminder",
                        "channel": "Email",
                        "purpose": "Payment Reminder",
                        "timing": "Day 0",
                    },
                    {"id": "w1", "kind": "Wait", "title": "Wait 48 hours", "timing": "48 Hours After"},
                    {
                        "id": "c2",
                        "kind": "Communication",
                        "title": "SMS reminder",
                        "channel": "SMS",
                        "purpose": "Payment Reminder",
                    },
                    {"id": "r1", "kind": "AI Reassessment", "title": "Reassess engagement"},
                    {"id": "o1", "kind": "Outcome", "title": "Continue or escalate"},
                ],
                "ai_context": [
                    {"label": "Preferred channel", "value": "Email then SMS"},
                    {"label": "Risk band", "value": "Low–Medium"},
                ],
                "versions": [
                    {"version": 3, "date": "10 Sep 2026", "note": "Approved for production"},
                    {"version": 2, "date": "08 Sep 2026", "note": "SMS step timing tightened"},
                ],
                "approved_by": "Daniya Shaikh",
                "approval_date": "10 Sep 2026",
            },
            {
                "code": "pp-ptp-follow-up",
                "name": "Promise-to-Pay Follow-Up",
                "client_code": "paypal",
                "status": "Under Review",
                "origin": "Human Modified",
                "version": 2,
                "summary": "Follow up on active promises with SMS before promise date, then reassess.",
                "coverage": "Active PTP accounts",
                "segment": {"delinquency": "31–60 days", "balance_band": "$5K–$10K"},
                "steps": [
                    {"id": "t1", "kind": "Trigger", "title": "Promise captured"},
                    {
                        "id": "c1",
                        "kind": "Communication",
                        "title": "Promise reminder SMS",
                        "channel": "SMS",
                        "purpose": "Promise-to-Pay Follow-Up",
                        "timing": "3 Days Before",
                    },
                    {"id": "w1", "kind": "Wait", "title": "Wait for promise date"},
                    {
                        "id": "cond1",
                        "kind": "Condition",
                        "title": "Promise kept?",
                        "detail": "Payment received by promise date",
                    },
                    {"id": "h1", "kind": "Human Review", "title": "Broken promise review"},
                ],
                "ai_context": [{"label": "Exception", "value": "Broken promise escalates to review"}],
                "versions": [{"version": 2, "date": "12 Sep 2026", "note": "Human modified timing"}],
            },
            {
                "code": "ct-progressive-reminder",
                "name": "Progressive Reminder",
                "client_code": "canadian-tire",
                "status": "AI Proposed",
                "origin": "AI Proposed",
                "version": 1,
                "summary": "Four-step progressive SMS/email ladder for mid-stage retail balances.",
                "coverage": "Progressive reminder cohort",
                "segment": {"delinquency": "15–45 days"},
                "steps": [
                    {"id": "t1", "kind": "Trigger", "title": "File assigned"},
                    {
                        "id": "c1",
                        "kind": "Communication",
                        "title": "SMS reminder 1",
                        "channel": "SMS",
                        "purpose": "Payment Reminder",
                    },
                    {"id": "w1", "kind": "Wait", "title": "Wait 3 days"},
                    {
                        "id": "c2",
                        "kind": "Communication",
                        "title": "Email reminder 2",
                        "channel": "Email",
                        "purpose": "Payment Reminder",
                    },
                    {"id": "r1", "kind": "AI Reassessment", "title": "Channel preference check"},
                ],
                "ai_context": [{"label": "Proposal", "value": "Awaiting supervisor approval"}],
                "versions": [{"version": 1, "date": "11 Sep 2026", "note": "AI proposed"}],
            },
            {
                "code": "ns-early-stage",
                "name": "Early Stage Collection",
                "client_code": "northstar-utilities",
                "status": "Approved",
                "origin": "Human Modified",
                "version": 2,
                "summary": "Soft utility reminder path with hardship-aware reassessment.",
                "coverage": "Residential early stage",
                "segment": {"delinquency": "1–30 days", "language": "English"},
                "steps": [
                    {"id": "t1", "kind": "Trigger", "title": "Utility account overdue"},
                    {
                        "id": "c1",
                        "kind": "Communication",
                        "title": "Soft email",
                        "channel": "Email",
                        "purpose": "Payment Reminder",
                    },
                    {"id": "w1", "kind": "Wait", "title": "Wait 72 hours"},
                    {"id": "r1", "kind": "AI Reassessment", "title": "Hardship screen"},
                    {"id": "o1", "kind": "Outcome", "title": "Continue monitoring"},
                ],
                "ai_context": [{"label": "Domain", "value": "Utilities hardship-aware"}],
                "versions": [
                    {"version": 2, "date": "10 Sep 2026", "note": "Approved"},
                    {"version": 1, "date": "07 Sep 2026", "note": "Created"},
                ],
                "approved_by": "Sarah",
                "approval_date": "10 Sep 2026",
            },
            {
                "code": "pp-escalated",
                "name": "Escalated Collection",
                "client_code": "paypal",
                "status": "Active",
                "origin": "Human Modified",
                "version": 4,
                "summary": "High-balance escalation with settlement offer gated by human review.",
                "coverage": "High balance · escalated",
                "segment": {"balance_band": "$10K+", "delinquency": "90+ days"},
                "steps": [
                    {"id": "t1", "kind": "Trigger", "title": "Escalation threshold met"},
                    {
                        "id": "c1",
                        "kind": "Communication",
                        "title": "Final notice email",
                        "channel": "Email",
                        "purpose": "Final Notice",
                    },
                    {"id": "h1", "kind": "Human Review", "title": "Settlement approval"},
                    {
                        "id": "c2",
                        "kind": "Communication",
                        "title": "Settlement offer",
                        "channel": "Email",
                        "purpose": "Settlement Offer",
                    },
                    {"id": "o1", "kind": "Outcome", "title": "Case resolved or continue"},
                ],
                "ai_context": [{"label": "Gate", "value": "Settlement requires human review"}],
                "versions": [{"version": 4, "date": "09 Sep 2026", "note": "Active"}],
                "approved_by": "Daniya Shaikh",
                "approval_date": "09 Sep 2026",
            },
        ]
        for item in demo:
            if db.query(PayflowStrategyModel).filter(PayflowStrategyModel.code == item["code"]).first():
                continue
            client = clients.get(item["client_code"])
            if not client:
                continue
            db.add(
                PayflowStrategyModel(
                    code=item["code"],
                    name=item["name"],
                    client_id=client.id,
                    status=item["status"],
                    origin=item["origin"],
                    version=item["version"],
                    summary=item["summary"],
                    coverage=item.get("coverage"),
                    segment=item.get("segment"),
                    steps=item["steps"],
                    ai_context=item.get("ai_context"),
                    versions=item.get("versions"),
                    approved_by=item.get("approved_by"),
                    approval_date=item.get("approval_date"),
                    created_by="PayFlow Seeder",
                )
            )
        db.flush()

    def _seed_payflow_demo_communications(self, db: Session) -> None:
        clients = {c.code: c for c in db.query(PayflowClientModel).all()}
        accounts = {a.account_reference: a for a in db.query(PayflowAccountModel).all()}
        reviews = {r.code: r for r in db.query(PayflowHumanReviewModel).all()}
        demo = [
            {
                "code": "cm-90412",
                "client_code": "paypal",
                "account_reference": "PP-10482",
                "channel": "Email",
                "purpose": "Payment Reminder",
                "status": "Payment Link Clicked",
                "workflow_name": "Early Stage Collection",
                "engagement": "Clicked payment link",
                "date_bucket": "Today",
                "date_label": "12 Sep 2026",
                "time_label": "09:42",
                "subject": "A quick reminder about your PayPal balance",
                "body_lines": [
                    "Hi John,",
                    "Your account PP-10482 has an outstanding balance of $4,250.",
                    "You can review and pay securely using the link below.",
                ],
                "payment_link": True,
                "why_message": "Early-stage reminder selected after partial payment and email preference.",
                "why_channel": "Email is the customer's preferred channel for this segment.",
                "why_timing": "Sent after the configured 48-hour reassessment window.",
                "events": [
                    {"at": "12 Sep · 09:42", "label": "Email sent"},
                    {"at": "12 Sep · 09:55", "label": "Delivered"},
                    {"at": "12 Sep · 10:12", "label": "Opened"},
                    {"at": "12 Sep · 10:18", "label": "Payment link clicked"},
                ],
                "balance": 4250,
            },
            {
                "code": "cm-90413",
                "client_code": "paypal",
                "account_reference": "PP-11021",
                "channel": "SMS",
                "purpose": "Promise-to-Pay Follow-Up",
                "status": "Opened / Read",
                "workflow_name": "Promise-to-Pay Follow-Up",
                "engagement": "Message read",
                "date_bucket": "Today",
                "date_label": "12 Sep 2026",
                "time_label": "08:15",
                "subject": None,
                "body_lines": [
                    "Hi Sarah, reminder: your promise of $8,900 is due 28 Aug. Pay: {{payment_link}}",
                ],
                "payment_link": True,
                "why_message": "Promise reminder before the due date.",
                "why_channel": "SMS for time-sensitive PTP follow-up.",
                "why_timing": "3 days before promise date.",
                "events": [
                    {"at": "12 Sep · 08:15", "label": "SMS sent"},
                    {"at": "12 Sep · 08:16", "label": "Delivered"},
                    {"at": "12 Sep · 08:40", "label": "Read"},
                ],
                "balance": 8900,
            },
            {
                "code": "cm-90414",
                "client_code": "canadian-tire",
                "account_reference": "CT-20394",
                "channel": "SMS",
                "purpose": "Payment Reminder",
                "status": "Delivered",
                "workflow_name": "Progressive Reminder",
                "engagement": "Delivered, not opened",
                "date_bucket": "Today",
                "date_label": "12 Sep 2026",
                "time_label": "07:50",
                "body_lines": ["Emily, reminder about CT-20394. Outstanding $7,300. {{payment_link}}"],
                "payment_link": True,
                "why_message": "Progressive reminder step 2 of 4.",
                "why_channel": "SMS preferred for this portfolio.",
                "why_timing": "Scheduled after previous soft reminder.",
                "events": [
                    {"at": "12 Sep · 07:50", "label": "SMS sent"},
                    {"at": "12 Sep · 07:51", "label": "Delivered"},
                ],
                "balance": 7300,
            },
            {
                "code": "cm-90415",
                "client_code": "paypal",
                "account_reference": "PP-88831",
                "channel": "Email",
                "purpose": "Settlement Offer",
                "status": "Awaiting Governance",
                "workflow_name": "Escalated Collection",
                "engagement": None,
                "date_bucket": "Today",
                "date_label": "12 Sep 2026",
                "time_label": "06:40",
                "subject": "Settlement option for your PayPal account",
                "body_lines": [
                    "Hi David,",
                    "A settlement option may be available for account PP-88831.",
                    "This message is awaiting supervisor approval before send.",
                ],
                "payment_link": True,
                "why_message": "High-balance rule requires settlement review.",
                "why_channel": "Email for formal settlement language.",
                "why_timing": "Held until human review completes.",
                "events": [{"at": "12 Sep · 06:40", "label": "Prepared · awaiting governance"}],
                "balance": 12500,
                "review_code": "rev-1044",
            },
            {
                "code": "cm-90416",
                "client_code": "canadian-tire",
                "account_reference": "CT-22540",
                "channel": "Email",
                "purpose": "Payment Reminder",
                "status": "Failed",
                "workflow_name": "Escalated Collection",
                "engagement": "Delivery failed",
                "date_bucket": "Yesterday",
                "date_label": "11 Sep 2026",
                "time_label": "14:22",
                "subject": "Important update on your Canadian Tire account",
                "body_lines": ["Hi Priya, please review your account CT-22540."],
                "payment_link": False,
                "why_message": "Progressive reminder before dispute detection.",
                "why_channel": "Email was last successful channel.",
                "why_timing": "Scheduled reminder window.",
                "events": [
                    {"at": "11 Sep · 14:22", "label": "Email send attempted"},
                    {"at": "11 Sep · 14:23", "label": "Failed", "detail": "Mailbox unavailable"},
                ],
                "balance": 3200,
                "review_code": "rev-1045",
            },
            {
                "code": "cm-90417",
                "client_code": "canadian-tire",
                "account_reference": "CT-21877",
                "channel": "SMS",
                "purpose": "Payment Plan Reminder",
                "status": "Opened / Read",
                "workflow_name": "Payment Plan Monitoring",
                "engagement": "Read",
                "date_bucket": "Yesterday",
                "date_label": "11 Sep 2026",
                "time_label": "09:05",
                "body_lines": ["Robert, installment reminder for CT-21877. Next due soon."],
                "payment_link": True,
                "why_message": "Installment reminder on active plan.",
                "why_channel": "SMS for short payment-plan nudges.",
                "why_timing": "7 days before next installment.",
                "events": [
                    {"at": "11 Sep · 09:05", "label": "SMS sent"},
                    {"at": "11 Sep · 09:06", "label": "Delivered"},
                    {"at": "11 Sep · 09:20", "label": "Read"},
                ],
                "balance": 1900,
            },
            {
                "code": "cm-90418",
                "client_code": "northstar-utilities",
                "account_reference": "NS-30112",
                "channel": "Email",
                "purpose": "Payment Reminder",
                "status": "Delivered",
                "workflow_name": "Early Stage Collection",
                "engagement": "Delivered",
                "date_bucket": "Today",
                "date_label": "12 Sep 2026",
                "time_label": "10:05",
                "subject": "Friendly reminder from Northstar Utilities",
                "body_lines": [
                    "Hi Laura,",
                    "This is a soft reminder about account NS-30112.",
                    "Outstanding balance: $2,450.",
                ],
                "payment_link": True,
                "why_message": "First-time delinquency soft reminder.",
                "why_channel": "Email preferred for utility branding.",
                "why_timing": "Within early-stage window.",
                "events": [
                    {"at": "12 Sep · 10:05", "label": "Email sent"},
                    {"at": "12 Sep · 10:07", "label": "Delivered"},
                ],
                "balance": 2450,
            },
            {
                "code": "cm-90419",
                "client_code": "northstar-utilities",
                "account_reference": "NS-31450",
                "channel": "Email",
                "purpose": "Promise-to-Pay Follow-Up",
                "status": "Suppressed",
                "workflow_name": "Promise-to-Pay Follow-Up",
                "engagement": None,
                "date_bucket": "Today",
                "date_label": "12 Sep 2026",
                "time_label": "09:10",
                "subject": "About your payment promise",
                "body_lines": ["Hi Marcus, we paused outreach while hardship is reviewed."],
                "payment_link": False,
                "why_message": "Hardship signal suppressed further outreach.",
                "why_channel": "Email prepared but held.",
                "why_timing": "Suppressed by hardship review.",
                "events": [{"at": "12 Sep · 09:10", "label": "Suppressed", "detail": "Hardship review open"}],
                "balance": 5400,
                "review_code": "rev-1047",
            },
        ]
        for item in demo:
            if (
                db.query(PayflowCommunicationModel)
                .filter(PayflowCommunicationModel.code == item["code"])
                .first()
            ):
                continue
            client = clients.get(item["client_code"])
            account = accounts.get(item["account_reference"])
            if not client or not account:
                continue
            review = reviews.get(item["review_code"]) if item.get("review_code") else None
            db.add(
                PayflowCommunicationModel(
                    code=item["code"],
                    client_id=client.id,
                    account_id=account.id,
                    channel=item["channel"],
                    purpose=item["purpose"],
                    status=item["status"],
                    workflow_name=item.get("workflow_name"),
                    engagement=item.get("engagement"),
                    date_bucket=item.get("date_bucket"),
                    date_label=item.get("date_label"),
                    time_label=item.get("time_label"),
                    subject=item.get("subject"),
                    body_lines=item.get("body_lines"),
                    payment_link=bool(item.get("payment_link")),
                    why_message=item.get("why_message"),
                    why_channel=item.get("why_channel"),
                    why_timing=item.get("why_timing"),
                    events=item.get("events"),
                    balance=item.get("balance", 0),
                    review_id=review.id if review else None,
                )
            )
        db.flush()

    def _seed_payflow_menus(self, db: Session) -> None:
        unlock_keys = {
            "clients",
            "cases",
            "integrations",
            "review",
            "rules",
            "workflows",
            "comms",
        }
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

    def _seed_payflow_demo_notifications(self, db: Session) -> None:
        """Backfill in-app notifications for awaiting reviews / workflows / comms."""
        from app.modules.payflow.services.notification_service import PayflowNotificationService

        svc = PayflowNotificationService(db)
        svc.sync_awaiting_review_notifications()
        svc.sync_awaiting_workflow_notifications()
        svc.sync_awaiting_comm_notifications()


def run_seeder() -> None:
    DatabaseSeeder().run()
