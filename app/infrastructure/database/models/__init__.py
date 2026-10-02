"""
Database models — schema source of truth.

Platform identity / entitlement tables use explicit platform_* / *_entitlements /
*_assignments / navigation_* names. PayFlow operational auth uses payflow_* tables.
UUID only where needed: users + auth tokens.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.infrastructure.database.session import Base


# ---------------------------------------------------------------------------
# PLATFORM — MAIN
# ---------------------------------------------------------------------------


class PlatformRoleModel(Base):
    """Platform-level roles only (Super Admin / User). Not PayFlow roles."""

    __tablename__ = "platform_roles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    users = relationship("UserModel", back_populates="role")


class OrganizationModel(Base):
    __tablename__ = "organizations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    is_internal: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    users = relationship("UserModel", back_populates="organization")
    product_links = relationship(
        "OrganizationProductEntitlementModel", back_populates="organization"
    )


class ProductModel(Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="draft", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    organization_links = relationship(
        "OrganizationProductEntitlementModel", back_populates="product"
    )
    user_links = relationship("UserProductAssignmentModel", back_populates="product")


class UserModel(Base):
    """Shared platform identity — UUID so user ids are not guessable."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    organization_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("organizations.id"), nullable=False
    )
    role_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("platform_roles.id"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), default="invited", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    organization = relationship("OrganizationModel", back_populates="users")
    role = relationship("PlatformRoleModel", back_populates="users")
    product_links = relationship(
        "UserProductAssignmentModel", back_populates="user", cascade="all, delete-orphan"
    )
    refresh_tokens = relationship(
        "RefreshTokenModel", back_populates="user", cascade="all, delete-orphan"
    )
    payflow_membership = relationship(
        "PayflowUserMembershipModel",
        back_populates="user",
        uselist=False,
        cascade="all, delete-orphan",
    )

    @property
    def role_code(self) -> str:
        return self.role.code if self.role else ""


class NavigationSectionModel(Base):
    """Sidebar sections — platform (product_code='') or product-scoped (e.g. PAYFLOW)."""

    __tablename__ = "navigation_sections"
    __table_args__ = (
        UniqueConstraint("product_code", "key", name="uq_navigation_section_product_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    label: Mapped[str] = mapped_column(String(128), nullable=False)
    context: Mapped[str] = mapped_column(String(64), nullable=False)
    product_code: Mapped[str] = mapped_column(String(64), default="", nullable=False, index=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    items = relationship(
        "NavigationItemModel",
        back_populates="section",
        cascade="all, delete-orphan",
        order_by="NavigationItemModel.sort_order",
    )


class NavigationItemModel(Base):
    __tablename__ = "navigation_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    section_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("navigation_sections.id"), nullable=False
    )
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    label: Mapped[str] = mapped_column(String(128), nullable=False)
    route: Mapped[str] = mapped_column(String(255), nullable=False)
    icon: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    required_role_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    required_permission_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_coming_soon: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    section = relationship("NavigationSectionModel", back_populates="items")


# ---------------------------------------------------------------------------
# PLATFORM — LINK TABLES
# ---------------------------------------------------------------------------


class OrganizationProductEntitlementModel(Base):
    """Org may use a product (granted / revoked)."""

    __tablename__ = "organization_product_entitlements"
    __table_args__ = (
        UniqueConstraint("organization_id", "product_id", name="uq_org_product_entitlement"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    organization_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("organizations.id"), nullable=False
    )
    product_id: Mapped[int] = mapped_column(Integer, ForeignKey("products.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="granted", nullable=False)
    granted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    organization = relationship("OrganizationModel", back_populates="product_links")
    product = relationship("ProductModel", back_populates="organization_links")


class UserProductAssignmentModel(Base):
    """Person may open a product (within org entitlement)."""

    __tablename__ = "user_product_assignments"
    __table_args__ = (
        UniqueConstraint("user_id", "product_id", name="uq_user_product_assignment"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    product_id: Mapped[int] = mapped_column(Integer, ForeignKey("products.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    user = relationship("UserModel", back_populates="product_links")
    product = relationship("ProductModel", back_populates="user_links")


# ---------------------------------------------------------------------------
# PAYFLOW — MAIN
# ---------------------------------------------------------------------------


class PayflowRoleModel(Base):
    __tablename__ = "payflow_roles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    scope: Mapped[str] = mapped_column(String(32), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_built_in: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    permission_links = relationship(
        "PayflowRolePermissionModel", back_populates="role", cascade="all, delete-orphan"
    )
    memberships = relationship("PayflowUserMembershipModel", back_populates="role")


class PayflowPermissionModel(Base):
    __tablename__ = "payflow_permissions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    group_key: Mapped[str] = mapped_column(String(64), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    role_links = relationship("PayflowRolePermissionModel", back_populates="permission")
    assignment_links = relationship(
        "PayflowUserClientPermissionModel", back_populates="permission"
    )


class PayflowClientModel(Base):
    """PayFlow collection client (organization) with onboarding configuration."""

    __tablename__ = "payflow_clients"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str | None] = mapped_column(String(128), nullable=True)  # industry display
    status: Mapped[str] = mapped_column(String(32), default="draft", nullable=False)
    client_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    business_domain: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ai_mode: Mapped[str | None] = mapped_column(String(32), nullable=True)
    data_source_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    connection_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    crm_system_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    integration_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    environment: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sync_frequency: Mapped[str | None] = mapped_column(String(64), nullable=True)
    brand_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    logo_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    sender_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    email_from: Mapped[str | None] = mapped_column(String(255), nullable=True)
    sms_sender_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    channel_email: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    channel_sms: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    channel_whatsapp: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    governance_rules: Mapped[list | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=True
    )

    assignments = relationship("PayflowUserClientAssignmentModel", back_populates="client")
    portfolios = relationship(
        "PayflowPortfolioModel",
        back_populates="client",
        cascade="all, delete-orphan",
    )
    field_mappings = relationship(
        "PayflowClientFieldMappingModel",
        back_populates="client",
        cascade="all, delete-orphan",
    )
    accounts = relationship(
        "PayflowAccountModel",
        back_populates="client",
        cascade="all, delete-orphan",
    )
    rules = relationship(
        "PayflowRuleModel",
        back_populates="client",
        cascade="all, delete-orphan",
    )
    reviews = relationship(
        "PayflowHumanReviewModel",
        back_populates="client",
        cascade="all, delete-orphan",
    )
    strategies = relationship(
        "PayflowStrategyModel",
        back_populates="client",
        cascade="all, delete-orphan",
    )
    communications = relationship(
        "PayflowCommunicationModel",
        back_populates="client",
        cascade="all, delete-orphan",
    )


class PayflowPortfolioModel(Base):
    """Sub-client / portfolio under a PayFlow client."""

    __tablename__ = "payflow_portfolios"
    __table_args__ = (
        UniqueConstraint("client_id", "code", name="uq_payflow_portfolio_client_code"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    client_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("payflow_clients.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="onboarding", nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    client = relationship("PayflowClientModel", back_populates="portfolios")
    accounts = relationship("PayflowAccountModel", back_populates="portfolio")
    strategies = relationship("PayflowStrategyModel", back_populates="portfolio")


class PayflowAccountModel(Base):
    """Customer account under collection, with linked collection-case fields."""

    __tablename__ = "payflow_accounts"
    __table_args__ = (
        UniqueConstraint(
            "client_id", "account_reference", name="uq_payflow_account_client_reference"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    client_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("payflow_clients.id"), nullable=False, index=True
    )
    portfolio_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("payflow_portfolios.id"), nullable=True, index=True
    )
    customer_name: Mapped[str] = mapped_column(String(255), nullable=False)
    account_reference: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    case_reference: Mapped[str] = mapped_column(String(64), nullable=False)
    original_balance: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    outstanding_balance: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    recovered_balance: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    collection_status: Mapped[str] = mapped_column(String(64), default="Active", nullable=False)
    current_workflow: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_action: Mapped[str | None] = mapped_column(String(255), nullable=True)
    next_action: Mapped[str | None] = mapped_column(String(255), nullable=True)
    human_review: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    timeline: Mapped[list | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    client = relationship("PayflowClientModel", back_populates="accounts")
    portfolio = relationship("PayflowPortfolioModel", back_populates="accounts")
    reviews = relationship(
        "PayflowHumanReviewModel",
        back_populates="account",
        cascade="all, delete-orphan",
    )
    communications = relationship(
        "PayflowCommunicationModel",
        back_populates="account",
        cascade="all, delete-orphan",
    )


class PayflowStrategyModel(Base):
    """Collection strategy / visual workflow for a client (optionally portfolio-scoped)."""

    __tablename__ = "payflow_strategies"
    __table_args__ = (UniqueConstraint("code", name="uq_payflow_strategy_code"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    client_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("payflow_clients.id"), nullable=False, index=True
    )
    portfolio_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("payflow_portfolios.id"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="Under Review")
    origin: Mapped[str] = mapped_column(String(32), nullable=False, default="Human Modified")
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    coverage: Mapped[str | None] = mapped_column(String(128), nullable=True)
    segment: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    steps: Mapped[list | None] = mapped_column(JSON, nullable=True)
    ai_context: Mapped[list | None] = mapped_column(JSON, nullable=True)
    versions: Mapped[list | None] = mapped_column(JSON, nullable=True)
    approved_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    approval_date: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    client = relationship("PayflowClientModel", back_populates="strategies")
    portfolio = relationship("PayflowPortfolioModel", back_populates="strategies")


class PayflowCommunicationModel(Base):
    """Customer collection communication log entry."""

    __tablename__ = "payflow_communications"
    __table_args__ = (UniqueConstraint("code", name="uq_payflow_communication_code"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    client_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("payflow_clients.id"), nullable=False, index=True
    )
    account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("payflow_accounts.id"), nullable=False, index=True
    )
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    purpose: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(64), nullable=False, default="Sent")
    workflow_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    engagement: Mapped[str | None] = mapped_column(String(128), nullable=True)
    date_bucket: Mapped[str | None] = mapped_column(String(32), nullable=True)
    date_label: Mapped[str | None] = mapped_column(String(64), nullable=True)
    time_label: Mapped[str | None] = mapped_column(String(32), nullable=True)
    subject: Mapped[str | None] = mapped_column(String(512), nullable=True)
    body_lines: Mapped[list | None] = mapped_column(JSON, nullable=True)
    payment_link: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    why_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    why_channel: Mapped[str | None] = mapped_column(Text, nullable=True)
    why_timing: Mapped[str | None] = mapped_column(Text, nullable=True)
    events: Mapped[list | None] = mapped_column(JSON, nullable=True)
    balance: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    review_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("payflow_human_reviews.id"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    client = relationship("PayflowClientModel", back_populates="communications")
    account = relationship("PayflowAccountModel", back_populates="communications")
    review = relationship("PayflowHumanReviewModel")


class PayflowRuleModel(Base):
    """Governance rule — system-wide or client-scoped."""

    __tablename__ = "payflow_rules"
    __table_args__ = (UniqueConstraint("code", name="uq_payflow_rule_code"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    rule_type: Mapped[str] = mapped_column(String(32), nullable=False, default="Client Rule")
    client_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("payflow_clients.id"), nullable=True, index=True
    )
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    logic: Mapped[str] = mapped_column(String(8), nullable=False, default="ALL")
    conditions: Mapped[list | None] = mapped_column(JSON, nullable=True)
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="Draft")
    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    triggers_7d: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    applied_to: Mapped[list | None] = mapped_column(JSON, nullable=True)
    history: Mapped[list | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    client = relationship("PayflowClientModel", back_populates="rules")
    reviews = relationship("PayflowHumanReviewModel", back_populates="rule")


class PayflowHumanReviewModel(Base):
    """Exception queue item requiring supervisor judgement before a proposed action."""

    __tablename__ = "payflow_human_reviews"
    __table_args__ = (UniqueConstraint("code", name="uq_payflow_human_review_code"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    client_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("payflow_clients.id"), nullable=False, index=True
    )
    account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("payflow_accounts.id"), nullable=False, index=True
    )
    rule_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("payflow_rules.id"), nullable=True, index=True
    )
    priority: Mapped[str] = mapped_column(String(32), nullable=False, default="Normal")
    reason: Mapped[str] = mapped_column(String(255), nullable=False)
    condition_text: Mapped[str | None] = mapped_column(String(512), nullable=True)
    observed_value: Mapped[str | None] = mapped_column(String(512), nullable=True)
    proposed_action: Mapped[str] = mapped_column(String(255), nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    explanation: Mapped[list | None] = mapped_column(JSON, nullable=True)
    context: Mapped[list | None] = mapped_column(JSON, nullable=True)
    timeline: Mapped[list | None] = mapped_column(JSON, nullable=True)
    waiting_minutes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="Awaiting Review")
    assigned_supervisor: Mapped[str | None] = mapped_column(String(255), nullable=True)
    final_action: Mapped[str | None] = mapped_column(String(512), nullable=True)
    guidance: Mapped[str | None] = mapped_column(Text, nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    hold_until: Mapped[str | None] = mapped_column(String(128), nullable=True)
    history: Mapped[list | None] = mapped_column(JSON, nullable=True)
    days_past_due: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    client = relationship("PayflowClientModel", back_populates="reviews")
    account = relationship("PayflowAccountModel", back_populates="reviews")
    rule = relationship("PayflowRuleModel", back_populates="reviews")


class PayflowClientFieldMappingModel(Base):
    """CRM source field → PayFlow field mapping for a client."""

    __tablename__ = "payflow_client_field_mappings"
    __table_args__ = (
        UniqueConstraint(
            "client_id", "source_field", name="uq_payflow_client_source_field"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    client_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("payflow_clients.id"), nullable=False, index=True
    )
    source_field: Mapped[str] = mapped_column(String(128), nullable=False)
    payflow_field: Mapped[str | None] = mapped_column(String(128), nullable=True)
    sample_value: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="unmapped", nullable=False)
    is_required: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    client = relationship("PayflowClientModel", back_populates="field_mappings")


class PayflowUserMembershipModel(Base):
    """Links a platform user to their PayFlow role (one membership per user)."""

    __tablename__ = "payflow_user_memberships"
    __table_args__ = (UniqueConstraint("user_id", name="uq_payflow_user_membership"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    payflow_role_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("payflow_roles.id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    user = relationship("UserModel", back_populates="payflow_membership")
    role = relationship("PayflowRoleModel", back_populates="memberships")
    client_assignments = relationship(
        "PayflowUserClientAssignmentModel",
        back_populates="membership",
        cascade="all, delete-orphan",
    )


# ---------------------------------------------------------------------------
# PAYFLOW — LINK TABLES
# ---------------------------------------------------------------------------


class PayflowRolePermissionModel(Base):
    __tablename__ = "payflow_role_permissions"
    __table_args__ = (
        UniqueConstraint("payflow_role_id", "permission_id", name="uq_payflow_role_permission"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    payflow_role_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("payflow_roles.id"), nullable=False
    )
    permission_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("payflow_permissions.id"), nullable=False
    )

    role = relationship("PayflowRoleModel", back_populates="permission_links")
    permission = relationship("PayflowPermissionModel", back_populates="role_links")


class PayflowUserClientAssignmentModel(Base):
    """WHERE a client-scoped PayFlow user may work."""

    __tablename__ = "payflow_user_client_assignments"
    __table_args__ = (
        UniqueConstraint(
            "membership_id", "client_id", name="uq_payflow_membership_client"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    membership_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("payflow_user_memberships.id"), nullable=False
    )
    client_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("payflow_clients.id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    membership = relationship("PayflowUserMembershipModel", back_populates="client_assignments")
    client = relationship("PayflowClientModel", back_populates="assignments")
    permissions = relationship(
        "PayflowUserClientPermissionModel",
        back_populates="assignment",
        cascade="all, delete-orphan",
    )


class PayflowUserClientPermissionModel(Base):
    """WHAT a user may do inside an assigned client."""

    __tablename__ = "payflow_user_client_permissions"
    __table_args__ = (
        UniqueConstraint(
            "assignment_id", "permission_id", name="uq_payflow_assignment_permission"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    assignment_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("payflow_user_client_assignments.id"), nullable=False
    )
    permission_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("payflow_permissions.id"), nullable=False
    )

    assignment = relationship("PayflowUserClientAssignmentModel", back_populates="permissions")
    permission = relationship("PayflowPermissionModel", back_populates="assignment_links")


# ---------------------------------------------------------------------------
# AUTH SUPPORT
# ---------------------------------------------------------------------------


class AuthTokenModel(Base):
    __tablename__ = "auth_tokens"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    token_type: Mapped[str] = mapped_column(String(32), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class RefreshTokenModel(Base):
    __tablename__ = "refresh_tokens"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    jti: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    user = relationship("UserModel", back_populates="refresh_tokens")


class TokenDenylistModel(Base):
    __tablename__ = "token_denylist"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    jti: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class EmailLogModel(Base):
    """Outbound email archive — used when SMTP is not configured (Phase 1)."""

    __tablename__ = "email_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    to_email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    subject: Mapped[str] = mapped_column(String(500), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    email_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    action_link: Mapped[str | None] = mapped_column(Text, nullable=True)
    related_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(32), default="logged", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class PayflowNotificationModel(Base):
    """In-app notification inbox for PayFlow users (supervisors + ops admins)."""

    __tablename__ = "payflow_notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )
    notification_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    body: Mapped[str | None] = mapped_column(String(512), nullable=True)
    link: Mapped[str | None] = mapped_column(String(512), nullable=True)
    client_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("payflow_clients.id"), nullable=True, index=True
    )
    entity_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    entity_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    user = relationship("UserModel")
    client = relationship("PayflowClientModel")


# ---------------------------------------------------------------------------
# Backward-compatible aliases
# ---------------------------------------------------------------------------

RoleModel = PlatformRoleModel
OrganizationProductModel = OrganizationProductEntitlementModel
UserProductModel = UserProductAssignmentModel
MenuSectionModel = NavigationSectionModel
MenuItemModel = NavigationItemModel
PersonModel = UserModel
OrgProductEntitlementModel = OrganizationProductEntitlementModel
PersonProductAssignmentModel = UserProductAssignmentModel
