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
    setup_steps_remaining: int = 0
    onboarding: Optional[PayflowOnboardingProgressItem] = None
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
    final_action: Optional[str] = None
    guidance: Optional[str] = None
    rejection_reason: Optional[str] = None
    hold_until: Optional[str] = None
    history: List[ReviewHistoryItem] = Field(default_factory=list)
    can_decide: Optional[bool] = None
    can_modify: Optional[bool] = None
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
    statuses: List[str] = Field(default_factory=list)
    priorities: List[str] = Field(default_factory=list)
    reasons: List[str] = Field(default_factory=list)
    proposed_actions: List[str] = Field(default_factory=list)
    rejection_reasons: List[str] = Field(default_factory=list)
    waiting_buckets: List[str] = Field(default_factory=list)


class ApproveReviewRequest(BaseModel):
    note: Optional[str] = None


class ModifyReviewRequest(BaseModel):
    action: str
    guidance: Optional[str] = None


class RejectReviewRequest(BaseModel):
    reason: str
    comment: Optional[str] = None


class HoldReviewRequest(BaseModel):
    until: Optional[str] = None
    reason: Optional[str] = None


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
    channel: str
    purpose: str
    status: str
    workflow_name: Optional[str] = None
    engagement: Optional[str] = None
    date_bucket: Optional[str] = None
    date_label: Optional[str] = None
    time_label: Optional[str] = None
    subject: Optional[str] = None
    body_lines: List[str] = Field(default_factory=list)
    payment_link: bool = False
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
