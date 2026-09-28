from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session, joinedload

from app.core.exceptions import ForbiddenError
from app.infrastructure.database.models import (
    NavigationSectionModel,
    PayflowRolePermissionModel,
    PayflowUserClientAssignmentModel,
    PayflowUserClientPermissionModel,
    PayflowUserMembershipModel,
    UserModel,
)
from app.modules.platform.services.entitlement_service import has_product_access
from app.shared.enums import PayflowRoleCode, PayflowRoleScope


class AccessContextService:
    """Resolves Product entitlement → PayFlow role → scope → permissions."""

    def __init__(self, db: Session):
        self.db = db

    def _load_membership(self, user_id: UUID) -> PayflowUserMembershipModel | None:
        return (
            self.db.query(PayflowUserMembershipModel)
            .options(
                joinedload(PayflowUserMembershipModel.role),
                joinedload(PayflowUserMembershipModel.client_assignments).joinedload(
                    PayflowUserClientAssignmentModel.client
                ),
                joinedload(PayflowUserMembershipModel.client_assignments)
                .joinedload(PayflowUserClientAssignmentModel.permissions)
                .joinedload(PayflowUserClientPermissionModel.permission),
            )
            .filter(PayflowUserMembershipModel.user_id == user_id)
            .first()
        )

    def require_membership(self, user: UserModel) -> PayflowUserMembershipModel:
        if not has_product_access(self.db, user, "PAYFLOW"):
            raise ForbiddenError("PayFlow product entitlement required")
        membership = self._load_membership(user.id)
        if not membership or not membership.role:
            raise ForbiddenError("PayFlow role not assigned for this account")
        return membership

    def build_context(self, user: UserModel) -> dict[str, Any]:
        membership = self.require_membership(user)
        role = membership.role
        is_ops_admin = (
            role.scope == PayflowRoleScope.PLATFORM_WIDE.value
            or role.code == PayflowRoleCode.OPERATIONS_ADMIN.value
        )

        role_perm_codes = self._role_permission_codes(role.id)
        client_ids: list[int] = []
        permissions_by_client: dict[str, list[str]] = {}

        if not is_ops_admin:
            for assignment in membership.client_assignments:
                client_ids.append(assignment.client_id)
                perm_codes = [
                    link.permission.code
                    for link in assignment.permissions
                    if link.permission
                ]
                if not perm_codes:
                    perm_codes = list(role_perm_codes)
                permissions_by_client[str(assignment.client_id)] = perm_codes

        return {
            "product_code": "PAYFLOW",
            "user_id": str(user.id),
            "full_name": user.full_name,
            "email": user.email,
            "status": user.status,
            "role": {
                "id": role.id,
                "code": role.code,
                "name": role.name,
                "scope": role.scope,
                "description": role.description,
            },
            "is_operations_admin": is_ops_admin,
            "client_ids": client_ids,
            "permissions_by_client": permissions_by_client,
            "all_permissions": role_perm_codes
            if is_ops_admin
            else sorted({p for perms in permissions_by_client.values() for p in perms}),
        }

    def _role_permission_codes(self, role_id: int) -> list[str]:
        links = (
            self.db.query(PayflowRolePermissionModel)
            .options(joinedload(PayflowRolePermissionModel.permission))
            .filter(PayflowRolePermissionModel.payflow_role_id == role_id)
            .all()
        )
        return sorted(link.permission.code for link in links if link.permission)

    def is_operations_admin(self, user: UserModel) -> bool:
        membership = self.require_membership(user)
        role = membership.role
        return (
            role.scope == PayflowRoleScope.PLATFORM_WIDE.value
            or role.code == PayflowRoleCode.OPERATIONS_ADMIN.value
        )

    def can_see_client(self, user: UserModel, client_id: int) -> bool:
        if self.is_operations_admin(user):
            return True
        ctx = self.build_context(user)
        return client_id in ctx["client_ids"]

    def can(self, user: UserModel, permission: str, client_id: int | None = None) -> bool:
        if self.is_operations_admin(user):
            return True
        if client_id is None:
            return False
        ctx = self.build_context(user)
        return permission in ctx["permissions_by_client"].get(str(client_id), [])

    def menus_for_user(self, user: UserModel) -> dict:
        ctx = self.build_context(user)
        role_code = ctx["role"]["code"]
        is_admin = ctx["is_operations_admin"]

        sections = (
            self.db.query(NavigationSectionModel)
            .filter(NavigationSectionModel.product_code == "PAYFLOW")
            .order_by(NavigationSectionModel.sort_order)
            .all()
        )
        result = []
        for section in sections:
            items = []
            for item in sorted(section.items, key=lambda i: i.sort_order):
                if not item.is_active:
                    continue
                if item.required_role_code:
                    if is_admin:
                        # Ops Admin sees everything including admin-gated items
                        pass
                    elif item.required_role_code != role_code:
                        continue
                if (
                    item.required_permission_code
                    and not is_admin
                    and item.required_permission_code not in ctx["all_permissions"]
                ):
                    continue
                items.append(
                    {
                        "key": item.key,
                        "label": item.label,
                        "route": item.route,
                        "icon": item.icon,
                        "sort_order": item.sort_order,
                        "is_coming_soon": item.is_coming_soon,
                        "badge": "Soon" if item.is_coming_soon else None,
                    }
                )
            if items:
                result.append(
                    {
                        "key": section.key,
                        "label": section.label,
                        "sort_order": section.sort_order,
                        "items": items,
                    }
                )
        return {"sections": result}
