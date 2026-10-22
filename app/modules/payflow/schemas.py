import re
from datetime import datetime
from typing import Any, List, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

# Canadian postal code: A1A 1A1
_POSTAL_CODE_RE = re.compile(r"^[A-Z]\d[A-Z] \d[A-Z]\d$")


def _normalize_postal_code(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    cleaned = value.strip().upper()
    if not cleaned:
        return None
    if not _POSTAL_CODE_RE.fullmatch(cleaned):
        raise ValueError("Postal code must match format A1A 1A1")
    return cleaned


class PayflowRoleSummary(BaseModel):
    id: int
    code: str
    name: str
    scope: str
    description: Optional[str] = None


class AccessContextResponse(BaseModel):
    product_code: str
    user_id: str
    full_name: str
    email: str
    status: str
    role: PayflowRoleSummary
    is_operations_admin: bool
    client_ids: List[int]
    permissions_by_client: dict[str, List[str]]
    all_permissions: List[str]


class MenuItemResponse(BaseModel):
    key: str
    label: str
    route: str
    icon: Optional[str] = None
    sort_order: int
    is_coming_soon: bool = False
    badge: Optional[str] = None


class MenuSectionResponse(BaseModel):
    key: str
    label: str
    sort_order: int
    items: List[MenuItemResponse]


class MenusResponse(BaseModel):
    sections: List[MenuSectionResponse]


class CreatePayflowUserRequest(BaseModel):
    full_name: str = Field(min_length=2, max_length=255)
    email: EmailStr
    role_code: str
    status: Optional[str] = Field(
        default="invited",
        description="invited | active | disabled (Active/Inactive UI maps here)",
    )
    # Deprecated: client assignment is done on client create/edit only.
    client_ids: List[int] = Field(default_factory=list)
    # Deprecated: permissions come from the role when assigned to a client.
    permission_codes: List[str] = Field(default_factory=list)


class PayflowOnboardingStepItem(BaseModel):
    key: str
    label: str
    status: str
    informational: Optional[bool] = False


class PayflowOnboardingProgressItem(BaseModel):
    steps: List[PayflowOnboardingStepItem] = Field(default_factory=list)
    completed_required: int = 0
    total_required: int = 0
    percent: int = 0
    eligible_for_activation: bool = False


class PayflowClientItem(BaseModel):
    id: int
    code: str
    name: str
    category: Optional[str] = None
    industry: Optional[str] = None
    status: str
    status_label: Optional[str] = None
    client_type: Optional[str] = None
    client_type_label: Optional[str] = None
    business_domain: Optional[str] = None
    business_domain_label: Optional[str] = None
    ai_mode: Optional[str] = None
    ai_mode_label: Optional[str] = None
    data_source_type: Optional[str] = None
    connection_status: Optional[str] = None
    connection_status_label: Optional[str] = None
    supervisors: List[dict] = Field(default_factory=list)
    setup_incomplete: List[str] = Field(default_factory=list)
    setup_issues: List[str] = Field(default_factory=list)
    setup_steps_remaining: int = 0
    onboarding: Optional[PayflowOnboardingProgressItem] = None
    # How the client was first created: add_client | file_upload
    added_through: Optional[str] = None
    added_through_label: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class PayflowClientsListResponse(BaseModel):
    clients: List[PayflowClientItem]


class CreatePayflowClientRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    code: str = Field(min_length=1, max_length=64)
    client_type: Optional[str] = "Third Party"
    business_domain: Optional[str] = "Collections"
    industry: Optional[str] = None
    ai_mode: Optional[str] = "Supervised AI"
    data_source_type: Optional[str] = None
    crm_client_number: Optional[str] = None
    contact_name: Optional[str] = None
    contact_title: Optional[str] = None
    contact_email: Optional[str] = None
    contact_phone: Optional[str] = None
    address_line1: Optional[str] = None
    address_line2: Optional[str] = None
    city: Optional[str] = None
    province_state: Optional[str] = None
    country: Optional[str] = None
    postal_code: Optional[str] = None
    correspondence_language: Optional[str] = None
    currency_code: Optional[str] = None

    @field_validator("postal_code")
    @classmethod
    def postal_code_format(cls, value: Optional[str]) -> Optional[str]:
        return _normalize_postal_code(value)


class MappingUpdateItem(BaseModel):
    source_field: str
    payflow_field: Optional[str] = None
    sample_value: Optional[str] = None
    status: Optional[str] = None


class UpdatePayflowClientRequest(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    code: Optional[str] = Field(default=None, min_length=1, max_length=64)
    client_type: Optional[str] = None
    business_domain: Optional[str] = None
    industry: Optional[str] = None
    category: Optional[str] = None
    ai_mode: Optional[str] = None
    data_source_type: Optional[str] = None
    connection_status: Optional[str] = None
    crm_system_name: Optional[str] = None
    integration_ref: Optional[str] = None
    crm_client_number: Optional[str] = None
    contact_name: Optional[str] = None
    contact_title: Optional[str] = None
    contact_email: Optional[str] = None
    contact_phone: Optional[str] = None
    address_line1: Optional[str] = None
    address_line2: Optional[str] = None
    city: Optional[str] = None
    province_state: Optional[str] = None
    country: Optional[str] = None
    postal_code: Optional[str] = None
    correspondence_language: Optional[str] = None
    currency_code: Optional[str] = None
    crm_status: Optional[str] = None
    environment: Optional[str] = None
    sync_frequency: Optional[str] = None
    brand_name: Optional[str] = None
    sender_name: Optional[str] = None
    email_from: Optional[str] = None
    sms_sender_id: Optional[str] = None
    channels: Optional[dict] = None
    governance_rules: Optional[List[str]] = None
    # Portal options: allow_pay_in_full, allow_partial, allow_payment_plan, min amounts, plans.
    payment_settings: Optional[dict] = None
    mappings: Optional[List[MappingUpdateItem]] = None
    supervisor_user_ids: Optional[List[UUID]] = None

    @field_validator("postal_code")
    @classmethod
    def postal_code_format(cls, value: Optional[str]) -> Optional[str]:
        return _normalize_postal_code(value)


class CreatePortfolioRequest(BaseModel):
    name: str = Field(max_length=255, description="Portfolio name")
    code: str = Field(max_length=64, description="Portfolio code / reference")
    status: Optional[str] = "onboarding"
    description: Optional[str] = None
    crm_client_number: Optional[str] = None

    @field_validator("name")
    @classmethod
    def name_required(cls, value: str) -> str:
        cleaned = (value or "").strip()
        if not cleaned:
            raise ValueError("Portfolio name is required")
        return cleaned

    @field_validator("code")
    @classmethod
    def code_required(cls, value: str) -> str:
        cleaned = (value or "").strip()
        if not cleaned:
            raise ValueError("Portfolio code / reference is required")
        return cleaned


class UpdatePortfolioRequest(BaseModel):
    name: Optional[str] = Field(default=None, max_length=255)
    code: Optional[str] = Field(default=None, max_length=64)
    status: Optional[str] = None
    description: Optional[str] = None
    crm_client_number: Optional[str] = None

    @field_validator("name")
    @classmethod
    def name_if_set(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Portfolio name is required")
        return cleaned

    @field_validator("code")
    @classmethod
    def code_if_set(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Portfolio code / reference is required")
        return cleaned


class UpdatePayflowUserRequest(BaseModel):
    full_name: Optional[str] = Field(default=None, min_length=2, max_length=255)
    email: Optional[EmailStr] = None
    role_code: Optional[str] = None
    confirm_role_change: bool = False


class PayflowUserListItem(BaseModel):
    id: UUID
    full_name: str
    email: str
    role_code: str
    role_name: str
    role_scope: str
    status: str
    status_label: str
    assigned_clients: List[str]
    permission_profile: str
    role_permission_names: List[str] = Field(default_factory=list)
    role_permission_codes: List[str] = Field(default_factory=list)
    last_active: Optional[str] = None
    created_at: Optional[datetime] = None


class PayflowUserDetailResponse(PayflowUserListItem):
    organization_id: int
    organization_name: Optional[str] = None
    activation_link: Optional[str] = None


class PayflowUsersListResponse(BaseModel):
    users: List[PayflowUserListItem]
    total: int
    summary: dict


class PayflowRoleListItem(BaseModel):
    id: int
    code: str
    name: str
    scope: str
    description: Optional[str] = None
    is_built_in: bool
    permission_count: int
    permission_codes: List[str]
    user_count: int


class PayflowRolesListResponse(BaseModel):
    roles: List[PayflowRoleListItem]


class PayflowPermissionItem(BaseModel):
    code: str
    name: str
    group_key: str
    sort_order: int


class PayflowPermissionGroup(BaseModel):
    group_key: str
    group_label: str
    permissions: List[PayflowPermissionItem]


class PayflowPermissionsCatalogResponse(BaseModel):
    groups: List[PayflowPermissionGroup]


class CreatePayflowRoleRequest(BaseModel):
    name: str = Field(min_length=2, max_length=128)
    scope: str = Field(description="platform_wide | client_scoped")
    description: Optional[str] = None
    permission_codes: List[str] = Field(default_factory=list)


class UpdatePayflowRoleRequest(BaseModel):
    name: Optional[str] = Field(default=None, min_length=2, max_length=128)
    description: Optional[str] = None
    permission_codes: Optional[List[str]] = None


class MessageResponse(BaseModel):
    message: str
    activation_link: Optional[str] = None


class AccountTimelineEvent(BaseModel):
    label: str
    detail: str
    at: str


class PayflowAccountItem(BaseModel):
    id: int
    client_id: int
    client_code: Optional[str] = None
    client_name: Optional[str] = None
    portfolio_id: Optional[int] = None
    portfolio_name: Optional[str] = None
    customer_name: str
    account_reference: str
    case_reference: str
    original_balance: float
    outstanding_balance: float
    recovered_balance: float
    collection_status: str
    current_workflow: Optional[str] = None
    last_action: Optional[str] = None
    next_action: Optional[str] = None
    human_review: bool = False
    timeline: List[AccountTimelineEvent] = Field(default_factory=list)
    portfolio_code: Optional[str] = None
    customer_first_name: Optional[str] = None
    customer_last_name: Optional[str] = None
    date_of_birth: Optional[str] = None
    age_group: Optional[str] = None
    employment_status: Optional[str] = None
    income_band: Optional[str] = None
    education_level: Optional[str] = None
    customer_segment: Optional[str] = None
    address_line1: Optional[str] = None
    city: Optional[str] = None
    province_state: Optional[str] = None
    postal_code: Optional[str] = None
    country_code: Optional[str] = None
    region: Optional[str] = None
    email: Optional[str] = None
    phone_mobile: Optional[str] = None
    phone_work: Optional[str] = None
    language: Optional[str] = None
    currency_code: Optional[str] = None
    fee_amount: Optional[float] = None
    due_date: Optional[str] = None
    days_past_due: Optional[int] = None
    last_payment_amount: Optional[float] = None
    last_payment_date: Optional[str] = None
    last_payment_is_ptp: Optional[bool] = None
    ptp_code: Optional[str] = None
    ptp_amount: Optional[float] = None
    ptp_due_date: Optional[str] = None
    account_status: Optional[str] = None
    account_category: Optional[str] = None
    negative_balance_reason: Optional[str] = None
    crm_case_id: Optional[str] = None
    debtor_id: Optional[str] = None
    client_reference_number: Optional[str] = None
    product_code: Optional[str] = None
    date_listed: Optional[str] = None
    date_incurred: Optional[str] = None
    delinquency_date: Optional[str] = None
    paid_amount: Optional[float] = None
    settlement_rate: Optional[float] = None
    commission_rate: Optional[float] = None
    last_email_sent_date: Optional[str] = None
    last_sms_sent_date: Optional[str] = None
    last_contact_date: Optional[str] = None
    provincial_hold: Optional[bool] = None
    hold_days: Optional[int] = None
    email_consent: Optional[bool] = None
    source_updated_at: Optional[str] = None
    last_crm_refresh_at: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class AccountsIntakeSummary(BaseModel):
    files: int = 0
    latest_received_at: str = "—"
    latest_assigned_at: str = "—"
    accounts_in_files: int = 0


class PayflowAccountsListResponse(BaseModel):
    accounts: List[PayflowAccountItem]
    intake: AccountsIntakeSummary
    workflows: List[str] = Field(default_factory=list)
    statuses: List[str] = Field(default_factory=list)


class IntegrationIssueItem(BaseModel):
    at: str
    summary: str


class IntegrationMappingItem(BaseModel):
    source_field: str
    payflow_field: Optional[str] = None
    sample_value: Optional[str] = None
    status: str


class IntegrationMappingSummary(BaseModel):
    mapped: int = 0
    attention: int = 0
    unmapped: int = 0
    total: int = 0


class PayflowIntegrationItem(BaseModel):
    id: str
    name: str
    category: str
    client_id: Optional[int] = None
    client_name: str
    client_code: Optional[str] = None
    status: str
    last_activity: str
    last_successful: Optional[str] = None
    purpose: str
    data_source: Optional[str] = None
    issues: List[IntegrationIssueItem] = Field(default_factory=list)
    mappings: List[IntegrationMappingItem] = Field(default_factory=list)
    mapping_summary: Optional[IntegrationMappingSummary] = None


class IntegrationsSummary(BaseModel):
    connected: int = 0
    attention: int = 0
    pending: int = 0
    disconnected: int = 0


class PayflowIntegrationsListResponse(BaseModel):
    integrations: List[PayflowIntegrationItem]
    summary: IntegrationsSummary
    categories: List[str] = Field(default_factory=list)
    statuses: List[str] = Field(default_factory=list)


class IntegrationTestResponse(BaseModel):
    message: str
    integration_id: str
    status: str


class ReviewContextItem(BaseModel):
    label: str
    value: str


class ReviewTimelineItem(BaseModel):
    at: str
    label: str


class ReviewHistoryItem(BaseModel):
    at: str
    event: str
    detail: Optional[str] = None
    by: Optional[str] = None


class ReviewCommunicationItem(BaseModel):
    id: int
    code: str
    channel: str
    purpose: str
    status: str
    engagement: Optional[str] = None
    date_label: Optional[str] = None
    time_label: Optional[str] = None
    subject: Optional[str] = None


class ReviewPaymentContext(BaseModel):
    last_payment_amount: Optional[float] = None
    last_payment_date: Optional[str] = None
    last_payment_is_ptp: Optional[bool] = None
    ptp_code: Optional[str] = None
    ptp_amount: Optional[float] = None
    ptp_due_date: Optional[str] = None
    paid_amount: Optional[float] = None


class ReviewExceptionStep(BaseModel):
    id: Optional[str] = None
    title: Optional[str] = None
    kind: Optional[str] = None
    available: bool = True
    message: Optional[str] = None


class ReviewExecutionSummary(BaseModel):
    run_id: Optional[int] = None
    status: Optional[str] = None
    strategy_id: Optional[int] = None
    strategy_name: Optional[str] = None
    strategy_version: Optional[int] = None
    current_step_id: Optional[str] = None
    current_step_title: Optional[str] = None
    current_step_kind: Optional[str] = None
    next_step_id: Optional[str] = None
    next_step_title: Optional[str] = None
    next_scheduled_at: Optional[datetime] = None
    block_reason: Optional[str] = None
    workflow_definition_path: Optional[str] = None


class ReviewMissingRef(BaseModel):
    ref: str
    message: str


class ReviewWorkflowContext(BaseModel):
    strategy_id: Optional[int] = None
    strategy_name: Optional[str] = None
    strategy_code: Optional[str] = None
    strategy_available: Optional[bool] = None
    workflow_version: Optional[int] = None
    exception_step_id: Optional[str] = None
    exception_step: Optional[ReviewExceptionStep] = None
    current_workflow: Optional[str] = None
    collection_status: Optional[str] = None
    last_action: Optional[str] = None
    next_action: Optional[str] = None
    action_blocked: bool = False
    execution: Optional[ReviewExecutionSummary] = None
    workflow_definition_path: Optional[str] = None


class ReviewAuditItem(BaseModel):
    id: int
    event: str
    detail: Optional[str] = None
    actor_name: Optional[str] = None
    before_status: Optional[str] = None
    after_status: Optional[str] = None
    before_values: Optional[dict] = None
    after_values: Optional[dict] = None
    created_at: Optional[datetime] = None


class PayflowReviewItem(BaseModel):
    id: int
    code: str
    client_id: int
    client_code: Optional[str] = None
    client_name: Optional[str] = None
    account_id: int
    customer_name: Optional[str] = None
    account_reference: Optional[str] = None
    case_reference: Optional[str] = None
    portfolio_id: Optional[int] = None
    portfolio_name: Optional[str] = None
    original_balance: float = 0
    outstanding_balance: float = 0
    recovered_balance: float = 0
    days_past_due: int = 0
    current_workflow: Optional[str] = None
    priority: str
    reason: str
    rule_id: Optional[int] = None
    rule_code: Optional[str] = None
    rule_name: Optional[str] = None
    condition_text: Optional[str] = None
    observed_value: Optional[str] = None
    proposed_action: str
    confidence: Optional[float] = None
    explanation: List[str] = Field(default_factory=list)
    context: List[ReviewContextItem] = Field(default_factory=list)
    timeline: List[ReviewTimelineItem] = Field(default_factory=list)
    waiting_minutes: int = 0
    waiting_label: Optional[str] = None
    status: str
    assigned_supervisor: Optional[str] = None
    assigned_user_id: Optional[str] = None
    decided_by_user_id: Optional[str] = None
    decided_at: Optional[datetime] = None
    final_action: Optional[str] = None
    guidance: Optional[str] = None
    rejection_reason: Optional[str] = None
    hold_until: Optional[str] = None
    history: List[ReviewHistoryItem] = Field(default_factory=list)
    audit_trail: List[ReviewAuditItem] = Field(default_factory=list)
    communications: List[ReviewCommunicationItem] = Field(default_factory=list)
    payment_context: Optional[ReviewPaymentContext] = None
    workflow_context: Optional[ReviewWorkflowContext] = None
    available_actions: List[str] = Field(default_factory=list)
    missing_refs: List[ReviewMissingRef] = Field(default_factory=list)
    decision_version: Optional[int] = None
    strategy_id: Optional[int] = None
    workflow_version: Optional[int] = None
    exception_step_id: Optional[str] = None
    exception_step: Optional[ReviewExceptionStep] = None
    can_decide: Optional[bool] = None
    can_modify: Optional[bool] = None
    can_release: Optional[bool] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class ReviewsSummary(BaseModel):
    awaiting: int = 0
    high_priority: int = 0
    due_today: int = 0
    on_hold: int = 0


class PayflowReviewsListResponse(BaseModel):
    reviews: List[PayflowReviewItem]
    summary: ReviewsSummary
    page: int = 1
    page_size: int = 50
    total: int = 0
    total_pages: int = 0
    statuses: List[str] = Field(default_factory=list)
    priorities: List[str] = Field(default_factory=list)
    reasons: List[str] = Field(default_factory=list)
    proposed_actions: List[str] = Field(default_factory=list)
    rejection_reasons: List[str] = Field(default_factory=list)
    waiting_buckets: List[str] = Field(default_factory=list)
    sort_by: Optional[str] = None
    sort_dir: Optional[str] = None


class CreateHumanReviewRequest(BaseModel):
    account_id: int
    reason: str
    proposed_action: str
    priority: Optional[str] = "Normal"
    rule_id: Optional[int] = None
    condition_text: Optional[str] = None
    observed_value: Optional[str] = None
    confidence: Optional[float] = None
    explanation: List[str] = Field(default_factory=list)
    context: List[ReviewContextItem] = Field(default_factory=list)
    strategy_id: Optional[int] = None
    workflow_version: Optional[int] = None
    exception_step_id: Optional[str] = None
    assigned_user_id: Optional[str] = None
    assigned_supervisor: Optional[str] = None


class AssignReviewRequest(BaseModel):
    assigned_user_id: Optional[str] = None
    assigned_supervisor: Optional[str] = None


class ApproveReviewRequest(BaseModel):
    note: Optional[str] = None
    decision_version: Optional[int] = None


class ModifyReviewRequest(BaseModel):
    action: str
    guidance: Optional[str] = None
    decision_version: Optional[int] = None


class RejectReviewRequest(BaseModel):
    reason: str
    comment: Optional[str] = None
    decision_version: Optional[int] = None


class HoldReviewRequest(BaseModel):
    until: Optional[str] = None
    reason: Optional[str] = None
    decision_version: Optional[int] = None


class ReleaseReviewRequest(BaseModel):
    note: Optional[str] = None
    decision_version: Optional[int] = None


class RuleConditionItem(BaseModel):
    id: Optional[str] = None
    field: str
    operator: str
    value: str


class RuleHistoryItem(BaseModel):
    at: str
    change: str
    by: str


class RuleRecentTrigger(BaseModel):
    review_id: int
    review_code: str
    customer_name: Optional[str] = None
    account_reference: Optional[str] = None
    client_name: Optional[str] = None
    status: str
    waiting_minutes: int = 0


class PayflowRuleItem(BaseModel):
    id: int
    code: str
    name: str
    description: Optional[str] = None
    rule_type: str
    client_id: Optional[int] = None
    client_code: Optional[str] = None
    client_name: Optional[str] = None
    category: str
    logic: str
    conditions: List[RuleConditionItem] = Field(default_factory=list)
    condition_summary: Optional[str] = None
    action: str
    status: str
    created_by: Optional[str] = None
    triggers_7d: int = 0
    applied_to: List[str] = Field(default_factory=list)
    history: List[RuleHistoryItem] = Field(default_factory=list)
    can_edit: Optional[bool] = None
    recent_triggers: List[RuleRecentTrigger] = Field(default_factory=list)
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    last_updated_label: Optional[str] = None


class RulesSummary(BaseModel):
    active: int = 0
    system: int = 0
    client: int = 0
    triggers_7d: int = 0


class RuleFieldCatalogItem(BaseModel):
    label: str
    category: str
    type: str
    options: Optional[List[str]] = None


class PayflowRulesListResponse(BaseModel):
    rules: List[PayflowRuleItem]
    summary: RulesSummary
    can_create: bool = False
    categories: List[str] = Field(default_factory=list)
    actions: List[str] = Field(default_factory=list)
    fields: List[RuleFieldCatalogItem] = Field(default_factory=list)
    statuses: List[str] = Field(default_factory=list)
    types: List[str] = Field(default_factory=list)
    logics: List[str] = Field(default_factory=list)


class CreatePayflowRuleRequest(BaseModel):
    """Create or update a rule. When ``id`` is set, the same POST updates that rule."""

    id: Optional[int] = None
    name: str = Field(min_length=1, max_length=255)
    description: Optional[str] = None
    rule_type: str = "Client Rule"
    client_id: Optional[int] = None
    category: str
    logic: str = "ALL"
    conditions: List[RuleConditionItem] = Field(default_factory=list)
    action: str
    status: str = "Draft"


class StrategyNodeConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    channel: Optional[str] = None
    purpose: Optional[str] = None
    reference_event: Optional[str] = None
    amount: Optional[float] = None
    unit: Optional[str] = None
    direction: Optional[str] = None
    attribute: Optional[str] = None
    operator: Optional[str] = None
    value: Optional[str] = None
    action: Optional[str] = None
    outcome: Optional[str] = None
    note: Optional[str] = None
    template_id: Optional[str] = None


class StrategyStepItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: Optional[str] = None
    kind: str
    title: str
    origin: Optional[str] = None
    disabled: bool = False
    config: Optional[StrategyNodeConfig] = None
    next: Optional[str] = None
    yes: Optional[str] = None
    no: Optional[str] = None
    # Legacy flat fields (still accepted / returned for compatibility)
    channel: Optional[str] = None
    purpose: Optional[str] = None
    timing: Optional[str] = None
    detail: Optional[str] = None


class StrategySegment(BaseModel):
    age_band: Optional[str] = None
    postal_region: Optional[str] = None
    balance_band: Optional[str] = None
    delinquency: Optional[str] = None
    language: Optional[str] = None
    tenure: Optional[str] = None


class StrategyVersionItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: Optional[int] = None  # payflow_strategy_versions.id when from immutable table
    version: int
    date: str
    note: str
    status: Optional[str] = None
    origin: Optional[str] = None
    approved_by: Optional[str] = None
    approval_date: Optional[str] = None
    steps: Optional[List[Any]] = None
    segment: Optional[dict] = None
    changes: List[str] = Field(default_factory=list)
    reviewer_note: Optional[str] = None  # why the reviewer approved / edited (optional)


class StrategyStats(BaseModel):
    steps: int = 0
    branches: int = 0
    emails: int = 0
    sms: int = 0
    payment_actions: int = 0
    case_actions: int = 0


class StrategyContextItem(BaseModel):
    label: str
    value: str


class StrategyMessageTemplate(BaseModel):
    id: str
    name: str
    channel: str
    purpose: str
    subject: Optional[str] = None
    body: str


class StrategyCatalog(BaseModel):
    channels: List[str] = Field(default_factory=list)
    message_purposes: List[str] = Field(default_factory=list)
    reference_events: List[str] = Field(default_factory=list)
    time_units: List[str] = Field(default_factory=list)
    time_directions: List[str] = Field(default_factory=list)
    condition_attributes: List[str] = Field(default_factory=list)
    condition_operators: List[str] = Field(default_factory=list)
    condition_values: dict[str, List[str]] = Field(default_factory=dict)
    case_actions: List[str] = Field(default_factory=list)
    payment_actions: List[str] = Field(default_factory=list)
    outcomes: List[str] = Field(default_factory=list)
    age_bands: List[str] = Field(default_factory=list)
    postal_regions: List[str] = Field(default_factory=list)
    balance_bands: List[str] = Field(default_factory=list)
    delinquency_bands: List[str] = Field(default_factory=list)
    language_preferences: List[str] = Field(default_factory=list)
    tenure_bands: List[str] = Field(default_factory=list)
    excluded_targeting_attributes: List[str] = Field(default_factory=list)
    message_templates: List[StrategyMessageTemplate] = Field(default_factory=list)


class PayflowStrategyItem(BaseModel):
    id: int
    code: str
    name: str
    client_id: int
    client_code: Optional[str] = None
    client_name: Optional[str] = None
    portfolio_id: Optional[int] = None
    portfolio_name: Optional[str] = None
    status: str
    origin: str
    source: str = "Human Created"
    version: int = 1
    summary: Optional[str] = None
    coverage: Optional[str] = None
    cases_covered: Optional[int] = None
    segment: dict = Field(default_factory=dict)
    entry_node_id: Optional[str] = None
    steps: List[StrategyStepItem] = Field(default_factory=list)
    stats: StrategyStats = Field(default_factory=StrategyStats)
    ai_context: List[StrategyContextItem] = Field(default_factory=list)
    ai_proposal_snapshot: Optional[dict] = None
    revision_of_id: Optional[int] = None  # Regenerate: the workflow this one was regenerated from
    versions: List[StrategyVersionItem] = Field(default_factory=list)
    approved_by: Optional[str] = None
    approval_date: Optional[str] = None
    reviewed_by: Optional[str] = None
    created_by: Optional[str] = None
    human_modified: bool = False
    awaiting_review: bool = False
    last_updated_label: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class StrategiesSummary(BaseModel):
    total: int = 0
    ai_proposed: int = 0
    draft: int = 0
    under_review: int = 0
    approved: int = 0
    active: int = 0
    inactive: int = 0
    awaiting_review: int = 0


class PayflowStrategiesListResponse(BaseModel):
    strategies: List[PayflowStrategyItem]
    summary: StrategiesSummary
    statuses: List[str] = Field(default_factory=list)
    step_kinds: List[str] = Field(default_factory=list)
    origins: List[str] = Field(default_factory=list)
    sources: List[str] = Field(default_factory=list)
    catalog: StrategyCatalog = Field(default_factory=StrategyCatalog)


class CreatePayflowStrategyRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    client_id: int
    portfolio_id: int
    summary: Optional[str] = None
    coverage: Optional[str] = None
    cases_covered: Optional[int] = None
    segment: Optional[dict] = None
    steps: List[StrategyStepItem] = Field(default_factory=list)
    entry_node_id: Optional[str] = None
    status: Optional[str] = None
    source: Optional[str] = None
    ai_context: Optional[List[StrategyContextItem]] = None


class UpdatePayflowStrategyRequest(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    summary: Optional[str] = None
    coverage: Optional[str] = None
    cases_covered: Optional[int] = None
    segment: Optional[dict] = None
    steps: Optional[List[StrategyStepItem]] = None
    entry_node_id: Optional[str] = None
    note: Optional[str] = Field(default=None, max_length=2000, description="Optional: why this edit was made")


class ApproveStrategyRequest(BaseModel):
    note: Optional[str] = Field(default=None, max_length=2000, description="Optional: why it was approved")


class ActivateStrategyRequest(BaseModel):
    note: Optional[str] = Field(
        default=None,
        max_length=2000,
        description="Optional: why this workflow is being activated",
    )


class RejectStrategyRequest(BaseModel):
    note: Optional[str] = None


class CommEventItem(BaseModel):
    at: str
    label: str
    detail: Optional[str] = None


class PayflowCommunicationItem(BaseModel):
    id: int
    code: str
    client_id: int
    client_code: Optional[str] = None
    client_name: Optional[str] = None
    account_id: int
    customer_name: Optional[str] = None
    account_reference: Optional[str] = None
    case_reference: Optional[str] = None
    case_status: Optional[str] = None
    channel: str
    purpose: str
    status: str
    workflow_name: Optional[str] = None
    engagement: Optional[str] = None
    engagement_tracking: Optional[str] = None
    date_bucket: Optional[str] = None
    date_label: Optional[str] = None
    time_label: Optional[str] = None
    subject: Optional[str] = None
    body_lines: List[str] = Field(default_factory=list)
    payment_link: bool = False
    payment_link_url: Optional[str] = None
    why_message: Optional[str] = None
    why_channel: Optional[str] = None
    why_timing: Optional[str] = None
    events: List[CommEventItem] = Field(default_factory=list)
    balance: float = 0
    review_id: Optional[int] = None
    drop_off_segment: Optional[str] = None
    brand_name: Optional[str] = None
    sender_name: Optional[str] = None
    email_from: Optional[str] = None
    sms_sender_id: Optional[str] = None
    provider: Optional[str] = None
    provider_message_id: Optional[str] = None
    recipient: Optional[str] = None
    provider_error: Optional[str] = None
    sent_at: Optional[datetime] = None
    delivered_at: Optional[datetime] = None
    opens_count: int = 0
    clicks_count: int = 0
    first_opened_at: Optional[datetime] = None
    last_opened_at: Optional[datetime] = None
    first_clicked_at: Optional[datetime] = None
    last_clicked_at: Optional[datetime] = None
    last_clicked_url: Optional[str] = None
    days_since_delivery_without_open: Optional[int] = None
    days_since_delivery_without_click: Optional[int] = None
    payment_initiated: bool = False
    payment_completed: bool = False
    eligibility_status: Optional[str] = None
    restriction_reason: Optional[str] = None
    rule_id: Optional[int] = None
    rule_name: Optional[str] = None
    rule_code: Optional[str] = None
    next_eligible_at: Optional[datetime] = None
    can_view_rule: bool = False
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class CommunicationsSummary(BaseModel):
    sent_today: int = 0
    delivered: int = 0
    engaged: int = 0
    clicks: int = 0
    failed: int = 0


class PayflowCommunicationsListResponse(BaseModel):
    communications: List[PayflowCommunicationItem]
    summary: CommunicationsSummary
    drop_off: dict[str, int] = Field(default_factory=dict)
    statuses: List[str] = Field(default_factory=list)
    channels: List[str] = Field(default_factory=list)
    purposes: List[str] = Field(default_factory=list)
    workflows: List[str] = Field(default_factory=list)
    drop_off_segments: List[str] = Field(default_factory=list)
    total: int = 0
    limit: int = 100
    offset: int = 0


class CommunicationEligibilitySnapshot(BaseModel):
    email: dict = Field(default_factory=dict)
    sms: dict = Field(default_factory=dict)
    latest_restriction_reason: Optional[str] = None
    latest_eligibility_status: Optional[str] = None
    next_eligible_at: Optional[datetime] = None
    rule_id: Optional[int] = None
    rule_name: Optional[str] = None
    rule_code: Optional[str] = None
    can_view_rule: bool = False
    communication_id: Optional[int] = None


class ManualSmsSendRequest(BaseModel):
    body: str = Field(min_length=1, max_length=1600)
    purpose: str = "Payment Reminder"
    include_payment_link: bool = False
    idempotency_key: Optional[str] = Field(default=None, max_length=64)


class ManualSmsSendResponse(BaseModel):
    skipped: Optional[str] = None
    communication_id: Optional[int] = None
    code: Optional[str] = None
    status: Optional[str] = None
    comm_status: Optional[str] = None
    recipient: Optional[str] = None
    provider_error: Optional[str] = None
    restriction_reason: Optional[str] = None


class ManualEmailSendRequest(BaseModel):
    subject: str = Field(min_length=1, max_length=500)
    body: str = Field(min_length=1, max_length=20000)
    purpose: str = "Payment Reminder"
    include_payment_link: bool = False
    idempotency_key: Optional[str] = Field(default=None, max_length=64)


class ManualEmailSendResponse(BaseModel):
    skipped: Optional[str] = None
    communication_id: Optional[int] = None
    code: Optional[str] = None
    status: Optional[str] = None
    comm_status: Optional[str] = None
    recipient: Optional[str] = None
    provider_error: Optional[str] = None
    restriction_reason: Optional[str] = None


class ResendCommunicationRequest(BaseModel):
    idempotency_key: Optional[str] = Field(default=None, max_length=64)


class PayflowPaymentLinkEventItem(BaseModel):
    at: Optional[str] = None
    label: Optional[str] = None
    detail: Optional[str] = None


class PayflowPaymentLinkItem(BaseModel):
    id: int
    token: str
    url: str
    status: str
    client_id: int
    account_id: int
    account_reference: Optional[str] = None
    customer_name: Optional[str] = None
    communication_id: Optional[int] = None
    communication_code: Optional[str] = None
    communication_channel: Optional[str] = None
    expires_at: Optional[datetime] = None
    revoked_at: Optional[datetime] = None
    revoked_by: Optional[str] = None
    completed_at: Optional[datetime] = None
    access_count: int = 0
    clicks_count: int = 0
    last_accessed_at: Optional[datetime] = None
    last_clicked_at: Optional[datetime] = None
    events: List[PayflowPaymentLinkEventItem] = Field(default_factory=list)
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    is_usable: bool = False


class PayflowPaymentLinksListResponse(BaseModel):
    payment_links: List[PayflowPaymentLinkItem]
    total: int = 0
    active_count: int = 0


class PayflowPaymentLinkPublicResolve(BaseModel):
    usable: bool
    status: str
    block_reason: Optional[str] = None
    expires_at: Optional[datetime] = None
    token: Optional[str] = None
    customer: dict = Field(default_factory=dict)
    case: dict = Field(default_factory=dict)
    brand: dict = Field(default_factory=dict)
    link_ref: Optional[str] = None
    payment_options: List[dict] = Field(default_factory=list)
    provider: dict = Field(default_factory=dict)


class PayflowPaymentInitiateRequest(BaseModel):
    option_id: str = Field(min_length=1, max_length=64)
    amount: Optional[float] = Field(default=None, gt=0)
    idempotency_key: Optional[str] = Field(default=None, max_length=128)


class PayflowPaymentAttemptItem(BaseModel):
    id: str
    status: str
    option_type: str
    plan_id: Optional[str] = None
    amount: float
    currency_code: str
    provider: str
    checkout_url: Optional[str] = None
    error_message: Optional[str] = None
    balance_at_initiation: Optional[float] = None
    stripe_session_id: Optional[str] = None
    reference: Optional[str] = None
    confirmed: bool = False
    events: List[dict] = Field(default_factory=list)
    created_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


class PayflowPaymentMockOutcomeRequest(BaseModel):
    outcome: str = Field(min_length=1, max_length=32)


class PayflowAccountPaymentsResponse(BaseModel):
    account_id: int
    balances: dict = Field(default_factory=dict)
    transactions: List[dict] = Field(default_factory=list)
    attempts: List[dict] = Field(default_factory=list)
    payment_plans: List[dict] = Field(default_factory=list)
    total_transactions: int = 0


class PayflowOperationalExceptionItem(BaseModel):
    id: int
    exception_type: str
    severity: str
    status: str
    retriable: bool = True
    client_id: Optional[int] = None
    client_name: Optional[str] = None
    account_id: Optional[int] = None
    account_reference: Optional[str] = None
    customer_name: Optional[str] = None
    case_reference: Optional[str] = None
    source_entity_type: Optional[str] = None
    source_entity_id: Optional[str] = None
    reason: str
    diagnostic_ref: Optional[str] = None
    link: Optional[str] = None
    dedupe_key: str
    acknowledged_by: Optional[str] = None
    acknowledged_at: Optional[datetime] = None
    resolved_by: Optional[str] = None
    resolved_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class PayflowOperationalExceptionsListResponse(BaseModel):
    exceptions: List[PayflowOperationalExceptionItem]
    total: int = 0
    open_count: int = 0
    acknowledged_count: int = 0
    resolved_count: int = 0


class ResolveOperationalExceptionRequest(BaseModel):
    note: Optional[str] = Field(default=None, max_length=1000)


class PayflowNotificationItem(BaseModel):
    id: int
    notification_type: str
    title: str
    body: Optional[str] = None
    link: Optional[str] = None
    client_id: Optional[int] = None
    entity_type: Optional[str] = None
    entity_id: Optional[str] = None
    read: bool = False
    read_at: Optional[datetime] = None
    created_at: Optional[datetime] = None


class PayflowNotificationsListResponse(BaseModel):
    notifications: List[PayflowNotificationItem]
    unread_count: int = 0


class DashboardKpiItem(BaseModel):
    id: str
    label: str
    value: float | int = 0
    display: str
    hint: Optional[str] = None
    tone: str = "neutral"
    href: Optional[str] = None


class DashboardAttentionItem(BaseModel):
    id: str
    label: str
    count: int = 0
    tone: str = "peach"
    href: Optional[str] = None


class DashboardFunnelStep(BaseModel):
    step: str
    label: str
    value: int = 0
    display: str
    rate: Optional[str] = None
    drop: Optional[str] = None
    bar: float = 0
    paid: bool = False
    href: Optional[str] = None


class DashboardOutcomeItem(BaseModel):
    id: str
    label: str
    value: float | int = 0
    display: str
    href: Optional[str] = None


class DashboardClientAttentionItem(BaseModel):
    client_id: int
    name: str
    reviews: int = 0
    flagged_accounts: int = 0
    detail: str = ""
    badge: str = ""
    tone: str = "tan"
    href: Optional[str] = None


class DashboardActivityItem(BaseModel):
    id: int
    text: str
    when: str = ""
    href: Optional[str] = None
    created_at: Optional[str] = None


class DashboardClientOption(BaseModel):
    id: int
    name: str
    code: str
    status: str


class PayflowDashboardResponse(BaseModel):
    description: str
    filters: dict = Field(default_factory=dict)
    clients: List[DashboardClientOption] = Field(default_factory=list)
    channels: List[str] = Field(default_factory=list)
    workflows: List[str] = Field(default_factory=list)
    kpis: List[DashboardKpiItem] = Field(default_factory=list)
    attention: List[DashboardAttentionItem] = Field(default_factory=list)
    funnel: List[DashboardFunnelStep] = Field(default_factory=list)
    outcomes: List[DashboardOutcomeItem] = Field(default_factory=list)
    clients_attention: List[DashboardClientAttentionItem] = Field(default_factory=list)
    activity: List[DashboardActivityItem] = Field(default_factory=list)


class PayflowImportErrorItem(BaseModel):
    record_id: str
    client: str
    sub_client: str
    field: str
    error: str
    status: str


class PayflowImportCounts(BaseModel):
    total: int = 0
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    failed: int = 0
    rejected: int = 0
    successful: int = 0


class PayflowImportRunItem(BaseModel):
    id: int
    kind: str
    file_name: str
    date_time: str
    uploaded_by: str
    status: str
    counts: PayflowImportCounts
    errors: List[PayflowImportErrorItem] = Field(default_factory=list)


class PayflowImportListResponse(BaseModel):
    imports: List[PayflowImportRunItem]


class PayflowImportPreviewRecord(BaseModel):
    id: str
    record_id: str
    client: str
    sub_client: str
    client_name: str
    sub_client_name: str
    action: str
    client_code: Optional[str] = None
    is_sub: bool = False
    current_balance: Optional[float] = None
    incoming_balance: Optional[float] = None
    note: Optional[str] = None
    setup_issues: List[str] = Field(default_factory=list)


class PayflowImportPreviewSummary(BaseModel):
    total: int = 0
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    failed: int = 0
    rejected: int = 0
    successful: int = 0
    new_clients: int = 0
    existing_clients: int = 0
    new_sub_clients: int = 0
    existing_sub_clients: int = 0


class PayflowImportPreviewResponse(BaseModel):
    ok: bool
    file_name: str
    status: str
    message: Optional[str] = None
    summary: PayflowImportPreviewSummary
    preview: List[PayflowImportPreviewRecord] = Field(default_factory=list)
    errors: List[PayflowImportErrorItem] = Field(default_factory=list)


# AI workflow suggestion


class PortfolioSegmentStats(BaseModel):
    accounts: int
    total_outstanding: float
    avg_outstanding: float
    avg_days_past_due: Optional[int] = None
    with_email_pct: int
    with_mobile_pct: int
    email_consent_pct: int
    languages: dict[str, int] = Field(default_factory=dict)


class PortfolioSegmentItem(BaseModel):
    segment_id: str
    label: str
    segment: dict[str, str]
    stats: PortfolioSegmentStats


class PortfolioSegmentsResponse(BaseModel):
    client_id: int
    portfolio_id: int
    portfolio_name: str
    total_accounts: int
    eligible_accounts: int
    excluded: dict[str, int]
    segments: List[PortfolioSegmentItem] = Field(default_factory=list)


class SuggestWorkflowsRequest(BaseModel):
    client_id: int
    portfolio_id: int


class SuggestWorkflowsResponse(BaseModel):
    client_id: int
    portfolio_id: int
    portfolio_name: str
    workflows: List[PayflowStrategyItem] = Field(default_factory=list)


class RegenerateWorkflowRequest(BaseModel):
    remarks: str = Field(min_length=3, max_length=2000, description="Reviewer remarks the AI should address")

    @field_validator("remarks")
    @classmethod
    def remarks_required(cls, value: str) -> str:
        cleaned = (value or "").strip()
        if len(cleaned) < 3:
            raise ValueError("Remarks are required")
        return cleaned


class PayflowWorkflowWorkerLogItem(BaseModel):
    id: int
    event_type: str
    level: str
    message: str
    detail: Optional[str] = None
    client_id: Optional[int] = None
    client_name: Optional[str] = None
    client_code: Optional[str] = None
    portfolio_id: Optional[int] = None
    portfolio_name: Optional[str] = None
    portfolio_code: Optional[str] = None
    account_id: Optional[int] = None
    account_reference: Optional[str] = None
    customer_name: Optional[str] = None
    strategy_id: Optional[int] = None
    strategy_name: Optional[str] = None
    strategy_code: Optional[str] = None
    run_id: Optional[int] = None
    job_id: Optional[int] = None
    step_id: Optional[str] = None
    created_at: Optional[datetime] = None
    event_count: Optional[int] = 1
    group_key: Optional[str] = None
    step_summary: Optional[str] = None
    related_events: Optional[List["PayflowWorkflowWorkerLogItem"]] = None


class PayflowWorkflowWorkerLogsResponse(BaseModel):
    items: List[PayflowWorkflowWorkerLogItem] = Field(default_factory=list)


class PayflowWorkflowStepRunItem(BaseModel):
    id: int
    step_id: str
    kind: str
    title: Optional[str] = None
    attempt: int
    status: str
    ui_status: Optional[str] = None
    scheduled_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    error: Optional[str] = None
    block_reason: Optional[str] = None
    result_json: Optional[dict[str, Any]] = None
    communication_id: Optional[int] = None


class PayflowWorkflowActivityItem(BaseModel):
    at: Optional[datetime] = None
    label: str
    kind: Optional[str] = None
    detail: Optional[str] = None
    ui_status: Optional[str] = None
    step_id: Optional[str] = None
    step_run_id: Optional[int] = None
    communication_id: Optional[int] = None
    block_reason: Optional[str] = None
    error: Optional[str] = None


class PayflowWorkflowCurrentStep(BaseModel):
    id: Optional[str] = None
    title: Optional[str] = None
    kind: Optional[str] = None


class PayflowWorkflowRunItem(BaseModel):
    id: int
    account_id: int
    strategy_id: int
    strategy_version: Optional[int] = None
    strategy_name: Optional[str] = None
    strategy_code: Optional[str] = None
    workflow_definition_path: Optional[str] = None
    client_id: Optional[int] = None
    portfolio_id: Optional[int] = None
    status: str
    current_step_id: Optional[str] = None
    current_step: Optional[PayflowWorkflowCurrentStep] = None
    next_step_id: Optional[str] = None
    next_step_title: Optional[str] = None
    next_scheduled_at: Optional[datetime] = None
    human_review_id: Optional[int] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    last_error: Optional[str] = None
    block_reason: Optional[str] = None
    deactivate_policy: Optional[str] = None
    step_runs: List[PayflowWorkflowStepRunItem] = Field(default_factory=list)
    activity: List[PayflowWorkflowActivityItem] = Field(default_factory=list)


class PayflowWorkflowRunsResponse(BaseModel):
    items: List[PayflowWorkflowRunItem] = Field(default_factory=list)


class PayflowWorkflowPaymentEventRequest(BaseModel):
    amount: Optional[float] = None
    payment_date: Optional[str] = None
    is_ptp: Optional[bool] = None
    note: Optional[str] = None
