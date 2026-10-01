from __future__ import annotations

from typing import Optional
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.core.exceptions import ConflictError, NotFoundError, ValidationAppError
from app.infrastructure.database.models import (
    OrganizationModel,
    OrganizationProductEntitlementModel,
    PayflowClientModel,
    PayflowPermissionModel,
    PayflowRoleModel,
    PayflowRolePermissionModel,
    PayflowUserClientAssignmentModel,
    PayflowUserMembershipModel,
    PlatformRoleModel,
    ProductModel,
    UserModel,
)
from app.modules.identity.service import IdentityService
from app.modules.payflow.membership import (
    ensure_payflow_membership_for_role,
    ensure_user_product_assignment,
)
from app.shared.enums import (
    EntitlementStatus,
    PayflowRoleCode,
    PayflowRoleScope,
    PlatformRole,
    UserStatus,
)

_STATUS_LABELS = {
    UserStatus.INVITED.value: "Invitation Pending",
    UserStatus.ACTIVE.value: "Active",
    UserStatus.DISABLED.value: "Inactive",
}


class PayflowUserService:
    def __init__(self, db: Session):
        self.db = db

    def _status_label(self, status: str) -> str:
        return _STATUS_LABELS.get(status, status)

    def _payflow_role(self, code: str) -> PayflowRoleModel:
        role = self.db.query(PayflowRoleModel).filter(PayflowRoleModel.code == code).first()
        if not role:
            raise NotFoundError(f"PayFlow role '{code}' not found — run seeder")
        return role

    def _platform_user_role(self) -> PlatformRoleModel:
        role = (
            self.db.query(PlatformRoleModel)
            .filter(PlatformRoleModel.code == PlatformRole.PLATFORM_USER.value)
            .first()
        )
        if not role:
            raise NotFoundError("Platform user role not found — run seeder")
        return role

    def _ensure_org_payflow_entitlement(self, organization_id: int) -> None:
        product = self.db.query(ProductModel).filter(ProductModel.code == "PAYFLOW").first()
        if not product:
            raise NotFoundError("PAYFLOW product not found")
        row = (
            self.db.query(OrganizationProductEntitlementModel)
            .filter(
                OrganizationProductEntitlementModel.organization_id == organization_id,
                OrganizationProductEntitlementModel.product_id == product.id,
            )
            .first()
        )
        if not row or row.status != EntitlementStatus.GRANTED.value:
            raise ValidationAppError(
                "Organization does not have an active PayFlow entitlement"
            )

    def _membership_query(self):
        return self.db.query(PayflowUserMembershipModel).options(
            joinedload(PayflowUserMembershipModel.role),
            joinedload(PayflowUserMembershipModel.user).joinedload(UserModel.organization),
            joinedload(PayflowUserMembershipModel.client_assignments).joinedload(
                PayflowUserClientAssignmentModel.client
            ),
            joinedload(PayflowUserMembershipModel.client_assignments).joinedload(
                PayflowUserClientAssignmentModel.permissions
            ),
        )

    def _permission_profile(self, membership: PayflowUserMembershipModel) -> str:
        role = membership.role
        if role.scope == PayflowRoleScope.PLATFORM_WIDE.value:
            return "Full Access"
        # Role permissions are the source of truth; client pages only assign where.
        role_perm_ids = {
            link.permission_id
            for link in self.db.query(PayflowRolePermissionModel)
            .filter(PayflowRolePermissionModel.payflow_role_id == role.id)
            .all()
        }
        if not membership.client_assignments:
            return f"{role.name} (standard)"
        profiles = []
        for assignment in membership.client_assignments:
            assigned_ids = {link.permission_id for link in assignment.permissions}
            if not assigned_ids or assigned_ids == role_perm_ids:
                profiles.append(f"{role.name} (standard)")
            else:
                profiles.append("Custom")
        if profiles and all(p == profiles[0] for p in profiles):
            return profiles[0]
        return "Custom"

    def _role_permission_names(self, role: PayflowRoleModel) -> list[str]:
        if role.scope == PayflowRoleScope.PLATFORM_WIDE.value:
            return ["Full Access"]
        links = (
            self.db.query(PayflowRolePermissionModel)
            .options(joinedload(PayflowRolePermissionModel.permission))
            .filter(PayflowRolePermissionModel.payflow_role_id == role.id)
            .all()
        )
        return sorted(
            {
                link.permission.name
                for link in links
                if link.permission and link.permission.name
            }
        )

    def _serialize(self, membership: PayflowUserMembershipModel) -> dict:
        user = membership.user
        role = membership.role
        is_platform = role.scope == PayflowRoleScope.PLATFORM_WIDE.value
        clients = (
            ["All Clients"]
            if is_platform
            else [
                a.client.name
                for a in membership.client_assignments
                if a.client
            ]
        )
        if not is_platform and not clients:
            clients = []
        return {
            "id": user.id,
            "full_name": user.full_name,
            "email": user.email,
            "role_code": role.code,
            "role_name": role.name,
            "role_scope": role.scope,
            "status": user.status,
            "status_label": self._status_label(user.status),
            "assigned_clients": clients if clients else (["None assigned"] if not is_platform else ["All Clients"]),
            "permission_profile": self._permission_profile(membership),
            "role_permission_names": self._role_permission_names(role),
            "last_active": None,
            "created_at": user.created_at,
            "organization_id": user.organization_id,
            "organization_name": user.organization.name if user.organization else None,
        }

    def list_users(
        self,
        *,
        search: Optional[str] = None,
        role_code: Optional[str] = None,
        status: Optional[str] = None,
        client_id: Optional[int] = None,
    ) -> dict:
        memberships = self._membership_query().all()
        users = []
        for m in memberships:
            item = self._serialize(m)
            if search:
                q = search.strip().lower()
                if q not in f"{item['full_name']} {item['email']}".lower():
                    continue
            if role_code and item["role_code"] != role_code:
                continue
            if status and item["status"] != status:
                continue
            if client_id is not None:
                if item["role_scope"] == PayflowRoleScope.PLATFORM_WIDE.value:
                    pass
                elif client_id not in [a.client_id for a in m.client_assignments]:
                    continue
            users.append(item)

        users.sort(key=lambda u: u["full_name"].lower())
        summary = self._summary(memberships)
        return {"users": users, "total": len(users), "summary": summary}

    def _summary(self, memberships: list[PayflowUserMembershipModel]) -> dict:
        total = len(memberships)
        roles = self.db.query(PayflowRoleModel).count()
        platform_wide = sum(
            1
            for m in memberships
            if m.role and m.role.scope == PayflowRoleScope.PLATFORM_WIDE.value
        )
        client_scoped = total - platform_wide
        active = sum(
            1 for m in memberships if m.user and m.user.status == UserStatus.ACTIVE.value
        )
        return {
            "total_users": total,
            "roles": roles,
            "platform_wide_access": platform_wide,
            "client_scoped_users": client_scoped,
            "active_users": active,
        }

    def get_user(self, user_id: UUID) -> dict:
        membership = (
            self._membership_query()
            .filter(PayflowUserMembershipModel.user_id == user_id)
            .first()
        )
        if not membership:
            raise NotFoundError("PayFlow user not found")
        return self._serialize(membership)

    def create_user(
        self,
        actor: UserModel,
        full_name: str,
        email: str,
        role_code: str,
        *,
        status: Optional[str] = None,
        client_ids: Optional[list[int]] = None,
        permission_codes: Optional[list[str]] = None,
    ) -> dict:
        role_code = role_code.strip().lower()
        pf_role = self._payflow_role(role_code)
        email_norm = email.lower().strip()
        if self.db.query(UserModel).filter(UserModel.email == email_norm).first():
            raise ConflictError("A user with this email already exists")

        self._ensure_org_payflow_entitlement(actor.organization_id)
        platform_role = self._platform_user_role()

        # New accounts always start invited so activation can set the password.
        # Inactive maps to disabled (no invite). UI "Active" still invites first.
        status_norm = (status or UserStatus.INVITED.value).strip().lower()
        if status_norm in ("inactive", "disabled"):
            user_status = UserStatus.DISABLED.value
        else:
            user_status = UserStatus.INVITED.value

        user = UserModel(
            full_name=full_name.strip(),
            email=email_norm,
            organization_id=actor.organization_id,
            role_id=platform_role.id,
            status=user_status,
            password_hash=None,
        )
        self.db.add(user)
        self.db.flush()

        ensure_user_product_assignment(self.db, user.id, "PAYFLOW")
        ensure_payflow_membership_for_role(self.db, user.id, pf_role.code)

        # Client assignment happens on client create/edit only.
        # Role permissions apply when a supervisor is assigned to a client.
        _ = client_ids
        _ = permission_codes

        self.db.commit()

        user = self.db.query(UserModel).filter(UserModel.id == user.id).first()
        link = None
        if user_status == UserStatus.INVITED.value:
            link = IdentityService(self.db).send_activation_invite(user)
        detail = self.get_user(user.id)
        detail["activation_link"] = link
        return detail

    def list_clients(self) -> dict:
        rows = (
            self.db.query(PayflowClientModel)
            .filter(PayflowClientModel.status == "active")
            .order_by(PayflowClientModel.name)
            .all()
        )
        return {
            "clients": [
                {
                    "id": c.id,
                    "code": c.code,
                    "name": c.name,
                    "category": c.category,
                    "status": c.status,
                }
                for c in rows
            ]
        }

    def update_user(
        self,
        user_id: UUID,
        *,
        full_name: Optional[str] = None,
        email: Optional[str] = None,
        role_code: Optional[str] = None,
        confirm_role_change: bool = False,
    ) -> dict:
        membership = (
            self._membership_query()
            .filter(PayflowUserMembershipModel.user_id == user_id)
            .first()
        )
        if not membership:
            raise NotFoundError("PayFlow user not found")
        user = membership.user

        if email is not None:
            email_norm = email.lower().strip()
            other = (
                self.db.query(UserModel)
                .filter(UserModel.email == email_norm, UserModel.id != user_id)
                .first()
            )
            if other:
                raise ConflictError("A user with this email already exists")
            user.email = email_norm

        if full_name is not None:
            user.full_name = full_name.strip()

        if role_code is not None:
            role_code = role_code.strip().lower()
            if role_code != membership.role.code:
                if not confirm_role_change:
                    raise ValidationAppError(
                        "Role change affects access. Set confirm_role_change=true to proceed."
                    )
                new_role = self._payflow_role(role_code)
                membership.payflow_role_id = new_role.id
                # Ops Admin / platform-wide: clear client assignments (no longer apply)
                if new_role.scope == PayflowRoleScope.PLATFORM_WIDE.value:
                    for assignment in list(membership.client_assignments):
                        self.db.delete(assignment)

        self.db.commit()
        return self.get_user(user_id)

    def resend_invitation(self, user_id: UUID) -> dict:
        membership = (
            self.db.query(PayflowUserMembershipModel)
            .options(joinedload(PayflowUserMembershipModel.user))
            .filter(PayflowUserMembershipModel.user_id == user_id)
            .first()
        )
        if not membership:
            raise NotFoundError("PayFlow user not found")
        user = membership.user
        if user.status != UserStatus.INVITED.value:
            raise ValidationAppError(
                "Resend Invitation is only available for Invitation Pending users"
            )
        link = IdentityService(self.db).send_activation_invite(user)
        return {"message": "Invitation resent", "activation_link": link}

    def deactivate_user(self, user_id: UUID, *, actor: UserModel) -> dict:
        membership = (
            self._membership_query()
            .filter(PayflowUserMembershipModel.user_id == user_id)
            .first()
        )
        if not membership:
            raise NotFoundError("PayFlow user not found")
        user = membership.user
        if user.id == actor.id:
            raise ValidationAppError("You cannot deactivate your own account")
        if user.status != UserStatus.ACTIVE.value:
            raise ValidationAppError("Only an Active user can be deactivated")
        # Soft-disable: keep membership, client assignments, and permissions.
        user.status = UserStatus.DISABLED.value
        self.db.commit()
        return self.get_user(user_id)

    def reactivate_user(self, user_id: UUID, *, actor: UserModel) -> dict:
        membership = (
            self._membership_query()
            .filter(PayflowUserMembershipModel.user_id == user_id)
            .first()
        )
        if not membership:
            raise NotFoundError("PayFlow user not found")
        user = membership.user
        if user.id == actor.id:
            raise ValidationAppError("You cannot change status on your own account this way")
        if user.status != UserStatus.DISABLED.value:
            raise ValidationAppError("Only an Inactive user can be reactivated")
        # Restore access; keep role, client assignments, and permissions intact.
        if user.password_hash:
            user.status = UserStatus.ACTIVE.value
            self.db.commit()
            return self.get_user(user_id)
        # Never activated (created Inactive) — invite so they can set a password.
        user.status = UserStatus.INVITED.value
        self.db.commit()
        link = IdentityService(self.db).send_activation_invite(user)
        detail = self.get_user(user_id)
        detail["activation_link"] = link
        return detail

    def list_roles(self) -> dict:
        roles = self.db.query(PayflowRoleModel).order_by(PayflowRoleModel.id).all()
        membership_counts = dict(
            self.db.query(
                PayflowUserMembershipModel.payflow_role_id,
                func.count(PayflowUserMembershipModel.id),
            )
            .group_by(PayflowUserMembershipModel.payflow_role_id)
            .all()
        )
        items = []
        for role in roles:
            links = (
                self.db.query(PayflowRolePermissionModel)
                .options(joinedload(PayflowRolePermissionModel.permission))
                .filter(PayflowRolePermissionModel.payflow_role_id == role.id)
                .all()
            )
            codes = sorted(link.permission.code for link in links if link.permission)
            items.append(
                {
                    "id": role.id,
                    "code": role.code,
                    "name": role.name,
                    "scope": role.scope,
                    "description": role.description,
                    "is_built_in": role.is_built_in,
                    "permission_count": len(codes),
                    "permission_codes": codes,
                    "user_count": int(membership_counts.get(role.id, 0)),
                }
            )
        return {"roles": items}

    def list_permissions_catalog(self) -> dict:
        group_labels = {
            "client_case_access": "CLIENT & CASE ACCESS",
            "collection_operations": "COLLECTION OPERATIONS",
            "human_review": "HUMAN REVIEW",
            "governance": "GOVERNANCE",
            "analytics": "ANALYTICS",
            "client_configuration": "CLIENT CONFIGURATION",
        }
        rows = (
            self.db.query(PayflowPermissionModel)
            .order_by(PayflowPermissionModel.sort_order, PayflowPermissionModel.id)
            .all()
        )
        grouped: dict[str, list] = {}
        for row in rows:
            grouped.setdefault(row.group_key, []).append(
                {
                    "code": row.code,
                    "name": row.name,
                    "group_key": row.group_key,
                    "sort_order": row.sort_order,
                }
            )
        groups = []
        for key, perms in grouped.items():
            groups.append(
                {
                    "group_key": key,
                    "group_label": group_labels.get(key, key.replace("_", " ").upper()),
                    "permissions": perms,
                }
            )
        # Stable order by first permission sort_order
        groups.sort(key=lambda g: g["permissions"][0]["sort_order"] if g["permissions"] else 0)
        return {"groups": groups}

    def _slug_role_code(self, name: str) -> str:
        base = "".join(ch.lower() if ch.isalnum() else "_" for ch in name.strip())
        base = "_".join(part for part in base.split("_") if part) or "custom_role"
        code = base[:60]
        existing = {
            r.code for r in self.db.query(PayflowRoleModel).all()
        }
        if code not in existing:
            return code
        n = 2
        while f"{code}_{n}" in existing:
            n += 1
        return f"{code}_{n}"

    def create_role(
        self,
        *,
        name: str,
        scope: str,
        description: Optional[str],
        permission_codes: list[str],
    ) -> dict:
        scope_norm = scope.strip().lower().replace("-", "_")
        if scope_norm in ("platformwide", "platform_wide"):
            scope_norm = PayflowRoleScope.PLATFORM_WIDE.value
        elif scope_norm in ("clientscoped", "client_scoped"):
            scope_norm = PayflowRoleScope.CLIENT_SCOPED.value
        else:
            raise ValidationAppError("Scope must be platform_wide or client_scoped")

        name_clean = name.strip()
        dup = (
            self.db.query(PayflowRoleModel)
            .filter(func.lower(PayflowRoleModel.name) == name_clean.lower())
            .first()
        )
        if dup:
            raise ConflictError("A role with this name already exists")

        all_perms = {
            p.code: p for p in self.db.query(PayflowPermissionModel).all()
        }
        if scope_norm == PayflowRoleScope.PLATFORM_WIDE.value:
            selected = list(all_perms.values())
        else:
            if not permission_codes:
                raise ValidationAppError("Select at least one permission for a client-scoped role")
            selected = []
            for code in permission_codes:
                perm = all_perms.get(code)
                if not perm:
                    raise ValidationAppError(f"Unknown permission code: {code}")
                selected.append(perm)

        role = PayflowRoleModel(
            code=self._slug_role_code(name_clean),
            name=name_clean,
            scope=scope_norm,
            description=(description or "").strip() or None,
            is_built_in=False,
        )
        self.db.add(role)
        self.db.flush()
        for perm in selected:
            self.db.add(
                PayflowRolePermissionModel(payflow_role_id=role.id, permission_id=perm.id)
            )
        self.db.commit()
        return self.list_roles()

    def update_role(
        self,
        role_id: int,
        *,
        name: Optional[str] = None,
        description: Optional[str] = None,
        permission_codes: Optional[list[str]] = None,
    ) -> dict:
        role = self.db.query(PayflowRoleModel).filter(PayflowRoleModel.id == role_id).first()
        if not role:
            raise NotFoundError("Role not found")
        if role.is_built_in:
            raise ValidationAppError("Built-in roles cannot be edited")

        if name is not None:
            name_clean = name.strip()
            dup = (
                self.db.query(PayflowRoleModel)
                .filter(
                    func.lower(PayflowRoleModel.name) == name_clean.lower(),
                    PayflowRoleModel.id != role_id,
                )
                .first()
            )
            if dup:
                raise ConflictError("A role with this name already exists")
            role.name = name_clean

        if description is not None:
            role.description = description.strip() or None

        if permission_codes is not None:
            if role.scope == PayflowRoleScope.PLATFORM_WIDE.value:
                raise ValidationAppError("Platform-wide roles always hold all permissions")
            if not permission_codes:
                raise ValidationAppError("Select at least one permission")
            all_perms = {
                p.code: p for p in self.db.query(PayflowPermissionModel).all()
            }
            selected_ids = []
            for code in permission_codes:
                perm = all_perms.get(code)
                if not perm:
                    raise ValidationAppError(f"Unknown permission code: {code}")
                selected_ids.append(perm.id)
            existing = (
                self.db.query(PayflowRolePermissionModel)
                .filter(PayflowRolePermissionModel.payflow_role_id == role.id)
                .all()
            )
            for link in existing:
                self.db.delete(link)
            self.db.flush()
            for pid in selected_ids:
                self.db.add(
                    PayflowRolePermissionModel(payflow_role_id=role.id, permission_id=pid)
                )

        self.db.commit()
        return self.list_roles()

    def delete_role(self, role_id: int) -> dict:
        role = self.db.query(PayflowRoleModel).filter(PayflowRoleModel.id == role_id).first()
        if not role:
            raise NotFoundError("Role not found")
        if role.is_built_in:
            raise ValidationAppError("Built-in roles cannot be deleted")
        users = (
            self.db.query(PayflowUserMembershipModel)
            .filter(PayflowUserMembershipModel.payflow_role_id == role_id)
            .count()
        )
        if users > 0:
            raise ValidationAppError("Cannot delete a role that is assigned to users")
        links = (
            self.db.query(PayflowRolePermissionModel)
            .filter(PayflowRolePermissionModel.payflow_role_id == role_id)
            .all()
        )
        for link in links:
            self.db.delete(link)
        self.db.delete(role)
        self.db.commit()
        return {"message": "Role deleted"}
