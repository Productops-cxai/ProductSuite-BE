from datetime import datetime
from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field


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
    client_ids: List[int] = Field(default_factory=list)
    permission_codes: List[str] = Field(default_factory=list)


class PayflowClientItem(BaseModel):
    id: int
    code: str
    name: str
    category: Optional[str] = None
    status: str


class PayflowClientsListResponse(BaseModel):
    clients: List[PayflowClientItem]


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
