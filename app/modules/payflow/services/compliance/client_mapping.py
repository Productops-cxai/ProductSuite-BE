"""Which compliance rules belong to a client (FR-10).

Rules follow the client's jurisdiction automatically. Mandatory rules always
apply and cannot be switched off. Optional rules (Client Configurable, Advisory)
are on by default and each client may switch its own off; that choice is kept
across updates and re-imports.

The association table is what a client's "Compliance" tab reads and where the
optional switches live. Enforcement does not depend on a row existing: a rule
with no row is treated as on, so a rule activated later is in force at once.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.exceptions import ForbiddenError, NotFoundError, ValidationAppError
from app.infrastructure.database.models import (
    PayflowClientComplianceRuleModel,
    PayflowClientModel,
    PayflowComplianceRuleModel,
    UserModel,
)
from app.modules.payflow.services.access_context_service import AccessContextService
from app.modules.payflow.services.compliance import catalog
from app.modules.payflow.services.compliance.rule_service import (
    ComplianceRuleService,
    serialize_rule,
)


class ClientComplianceService:
    def __init__(self, db: Session):
        self.db = db
        self.access = AccessContextService(db)

    def applicable_rules(self, client: PayflowClientModel) -> list[PayflowComplianceRuleModel]:
        """Rules for this client's country: every state's rules, because the client's
        customers may live in any of them. Never another client's own rules."""
        country = catalog.find_country(client.country)
        if not country:
            return []
        return (
            self.db.query(PayflowComplianceRuleModel)
            .filter(
                PayflowComplianceRuleModel.status != catalog.STATUS_ARCHIVED,
                PayflowComplianceRuleModel.country == country["name"],
                or_(
                    PayflowComplianceRuleModel.client_id.is_(None),
                    PayflowComplianceRuleModel.client_id == client.id,
                ),
            )
            .order_by(PayflowComplianceRuleModel.priority.desc(), PayflowComplianceRuleModel.id.asc())
            .all()
        )

    def sync_client(
        self,
        client: PayflowClientModel,
        *,
        actor: UserModel | None = None,
        actor_name: str = "PayFlow",
        commit: bool = False,
    ) -> dict[str, int]:
        """Associate newly applicable rules. Existing choices are never touched and
        no association is ever created twice (safe to run on every save / import)."""
        rules = self.applicable_rules(client)
        existing = {
            row.rule_id
            for row in self.db.query(PayflowClientComplianceRuleModel).filter(
                PayflowClientComplianceRuleModel.client_id == client.id
            )
        }
        added: list[PayflowComplianceRuleModel] = []
        for rule in rules:
            if rule.id in existing:
                continue
            self.db.add(
                PayflowClientComplianceRuleModel(
                    client_id=client.id,
                    rule_id=rule.id,
                    enabled=True,
                    source="auto",
                    updated_by=(actor.full_name if actor else actor_name),
                )
            )
            added.append(rule)
        if added:
            self.db.flush()
            ComplianceRuleService(self.db).audit(
                entity_type="client_mapping",
                entity_id=client.id,
                rule_name=client.name,
                client_id=client.id,
                event="Client rule mapping updated",
                actor=actor,
                actor_name=actor_name,
                reason=f"{len(added)} newly applicable rule(s) associated",
                before=None,
                after={"added_rule_codes": [r.code for r in added]},
            )
        if commit:
            self.db.commit()
        return {"added": len(added), "total": len(rules)}

    def _client_for(self, user: UserModel, client_id: int, *, permission: str) -> PayflowClientModel:
        client = self.db.get(PayflowClientModel, client_id)
        if not client:
            raise NotFoundError("Client not found")
        if not self.access.can_see_client(user, client_id) or not self.access.can(
            user, permission, client_id
        ):
            raise ForbiddenError("Client access denied")
        return client

    def list_for_client(self, user: UserModel, client_id: int) -> dict[str, Any]:
        client = self._client_for(user, client_id, permission="view_client")
        rows = {
            row.rule_id: row
            for row in self.db.query(PayflowClientComplianceRuleModel).filter(
                PayflowClientComplianceRuleModel.client_id == client.id
            )
        }
        items = []
        for rule in self.applicable_rules(client):
            row = rows.get(rule.id)
            mandatory = rule.enforcement == catalog.ENFORCEMENT_MANDATORY
            items.append(
                {
                    **serialize_rule(rule),
                    "associated": row is not None,
                    "enabled": True if mandatory else (row.enabled if row else True),
                    "locked": mandatory,
                    "in_force": rule.status == catalog.STATUS_ACTIVE
                    and (mandatory or (row.enabled if row else True)),
                    "inherited": rule.client_id is None,
                }
            )
        country = catalog.find_country(client.country)
        return {
            "client_id": client.id,
            "jurisdiction": {
                "country": country["name"] if country else None,
                "state": client.province_state,
                "city": client.city,
                "postal": client.postal_code,
            },
            "jurisdiction_resolved": country is not None,
            "message": None
            if country
            else "Set the client's country (United States or Canada) to resolve its compliance rules.",
            "rules": items,
        }

    def set_enabled(
        self,
        user: UserModel,
        client_id: int,
        rule_id: int,
        *,
        enabled: bool,
        reason: str | None,
    ) -> dict[str, Any]:
        client = self._client_for(user, client_id, permission="modify_governance")
        rule = self.db.get(PayflowComplianceRuleModel, rule_id)
        if not rule or rule.client_id not in (None, client.id):
            raise NotFoundError("Compliance rule not found for this client")
        if rule.enforcement == catalog.ENFORCEMENT_MANDATORY and not enabled:
            # No client-level change may take a mandatory rule out of force.
            raise ForbiddenError("A mandatory compliance rule cannot be disabled for a client")
        if not (reason or "").strip():
            raise ValidationAppError("A reason for the change is required")
        row = (
            self.db.query(PayflowClientComplianceRuleModel)
            .filter(
                PayflowClientComplianceRuleModel.client_id == client.id,
                PayflowClientComplianceRuleModel.rule_id == rule.id,
            )
            .first()
        )
        before = {"enabled": row.enabled if row else True}
        if row is None:
            row = PayflowClientComplianceRuleModel(client_id=client.id, rule_id=rule.id)
            self.db.add(row)
        row.enabled = bool(enabled)
        row.source = "manual"
        row.updated_by = user.full_name
        self.db.flush()
        ComplianceRuleService(self.db).audit(
            entity_type="client_mapping",
            entity_id=client.id,
            rule=rule,
            client_id=client.id,
            event=f"Client rule {'enabled' if enabled else 'disabled'}",
            actor=user,
            reason=reason,
            before=before,
            after={"enabled": row.enabled},
        )
        self.db.commit()
        return self.list_for_client(user, client_id)

    def resync(self, user: UserModel, client_id: int) -> dict[str, Any]:
        client = self._client_for(user, client_id, permission="modify_governance")
        result = self.sync_client(client, actor=user, commit=True)
        return {**result, **self.list_for_client(user, client_id)}

    def list_clients_for_rule(self, user: UserModel, rule_id: int) -> dict[str, Any]:
        """Clients associated with (or in scope of) a compliance rule — FR-05 reverse view."""
        ComplianceRuleService(self.db)._assert_view(user)
        rule = self.db.get(PayflowComplianceRuleModel, rule_id)
        if not rule or rule.status == catalog.STATUS_ARCHIVED:
            raise NotFoundError("Compliance rule not found")

        mappings = {
            row.client_id: row
            for row in self.db.query(PayflowClientComplianceRuleModel).filter(
                PayflowClientComplianceRuleModel.rule_id == rule.id
            )
        }

        if rule.client_id is not None:
            client_ids = {rule.client_id} | set(mappings.keys())
            clients = (
                self.db.query(PayflowClientModel)
                .filter(PayflowClientModel.id.in_(client_ids))
                .order_by(PayflowClientModel.name.asc())
                .all()
                if client_ids
                else []
            )
        else:
            country = catalog.find_country(rule.country)
            country_name = country["name"] if country else (rule.country or "")
            query = self.db.query(PayflowClientModel)
            if country_name:
                names = {country_name}
                if country:
                    names.add(str(country.get("code") or ""))
                    for alias in country.get("aliases") or []:
                        names.add(str(alias))
                names.discard("")
                query = query.filter(PayflowClientModel.country.in_(sorted(names)))
            clients = query.order_by(PayflowClientModel.name.asc()).all()

        mandatory = rule.enforcement == catalog.ENFORCEMENT_MANDATORY
        items: list[dict[str, Any]] = []
        for client in clients:
            if not self.access.can_see_client(user, client.id):
                continue
            row = mappings.get(client.id)
            enabled = True if mandatory else (row.enabled if row else True)
            items.append(
                {
                    "client_id": client.id,
                    "code": client.code,
                    "name": client.name,
                    "country": client.country,
                    "province_state": client.province_state,
                    "city": client.city,
                    "status": client.status,
                    "associated": row is not None,
                    "enabled": enabled,
                    "locked": mandatory,
                    "in_force": rule.status == catalog.STATUS_ACTIVE and enabled,
                    "source": row.source if row else None,
                }
            )

        return {
            "rule_id": rule.id,
            "rule_code": rule.code,
            "rule_name": rule.name,
            "client_scoped": rule.client_id is not None,
            "total": len(items),
            "associated_count": sum(1 for i in items if i["associated"]),
            "clients": items,
        }
