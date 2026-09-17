from datetime import datetime
from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator

from app.shared.enums import EntitlementStatus, PersonStatus, ProductStatus


class ProductSave(BaseModel):
    """Create when id is omitted; update when id is provided."""

    id: Optional[int] = None
    name: str = Field(..., min_length=1, max_length=255)
    code: Optional[str] = Field(None, min_length=2, max_length=64)
    description: Optional[str] = None
    status: ProductStatus = ProductStatus.DRAFT

    @model_validator(mode="after")
    def code_required_on_create(self):
        if self.id is None and not self.code:
            raise ValueError("code is required when creating a product")
        return self


class ProductResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    code: str
    description: Optional[str] = None
    status: str
    created_at: datetime
    updated_at: datetime


class OrganizationSave(BaseModel):
    """Create when id is omitted; update when id is provided."""

    id: Optional[int] = None
    name: str = Field(..., min_length=1, max_length=255)
    is_internal: bool = False


class OrganizationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    is_internal: bool
    created_at: datetime


class ProductSummary(BaseModel):
    id: int
    name: str
    code: str
    status: str


class OrgAccessSummary(BaseModel):
    id: int
    name: str
    has_access: bool


class OverviewResponse(BaseModel):
    registered_products_count: int
    active_products_count: int
    organizations_with_access_count: int
    organizations_total_count: int
    organizations_with_access_label: str
    products_summary: List[ProductSummary]
    org_access_summary: List[OrgAccessSummary]


class MenuItemResponse(BaseModel):
    key: str
    label: str
    route: str
    icon: Optional[str] = None
    sort_order: int
    is_coming_soon: bool
    badge: Optional[str] = None


class MenuSectionResponse(BaseModel):
    key: str
    label: str
    sort_order: int
    items: List[MenuItemResponse]


class MenusResponse(BaseModel):
    sections: List[MenuSectionResponse]


class ProductAccessItem(BaseModel):
    entitlement_id: Optional[int] = None
    organization_id: int
    organization_name: str
    product_id: int
    product_name: str
    product_code: str
    access_status: EntitlementStatus | str


class GrantAccessRequest(BaseModel):
    organization_id: int
    product_id: int


class PersonSave(BaseModel):
    """Create when id is omitted; update when id is provided."""

    id: Optional[UUID] = None
    full_name: str = Field(..., min_length=1, max_length=255)
    email: Optional[EmailStr] = None
    organization_id: int
    status: Optional[PersonStatus] = None
    product_ids: List[int] = Field(default_factory=list)

    @model_validator(mode="after")
    def email_required_on_create(self):
        if self.id is None and not self.email:
            raise ValueError("email is required when creating a person")
        return self


class AssignedProduct(BaseModel):
    id: int
    name: str
    code: str
    effectively_entitled: bool


class PersonResponse(BaseModel):
    id: UUID
    full_name: str
    email: EmailStr
    organization_id: int
    organization_name: str
    role: str
    status: str
    assigned_products: List[AssignedProduct]
    created_at: datetime


class AssignProductRequest(BaseModel):
    user_id: UUID
    product_id: int


class PersonIdRequest(BaseModel):
    user_id: UUID


class EnterProductResponse(BaseModel):
    product: ProductResponse
    message: str = "Product access granted"
