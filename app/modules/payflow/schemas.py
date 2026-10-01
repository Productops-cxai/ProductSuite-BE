from datetime import datetime
from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field, field_validator


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


class CreatePortfolioRequest(BaseModel):
    name: str = Field(max_length=255, description="Portfolio name")
    code: str = Field(max_length=64, description="Portfolio code / reference")
    status: Optional[str] = "onboarding"
    description: Optional[str] = None

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
