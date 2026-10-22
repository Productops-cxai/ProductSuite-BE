from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session, joinedload

from app.core.exceptions import NotFoundError
from app.infrastructure.database.models import (
    PayflowHumanReviewModel,
    PayflowNotificationModel,
    PayflowRoleModel,
    PayflowUserClientAssignmentModel,
    PayflowUserMembershipModel,
    UserModel,
)
from app.infrastructure.email.service import send_email
from app.modules.payflow.services.access_context_service import AccessContextService
from app.shared.enums import PayflowNotificationType, PayflowRoleCode, PayflowReviewStatus


class PayflowNotificationService:
    def __init__(self, db: Session):
        self.db = db
        self.access = AccessContextService(db)

    # ------------------------------------------------------------------ recipients
    def recipients_for_client(self, client_id: int | None = None) -> list[UserModel]:
        """Ops admins (all) + supervisors assigned to client (when client_id set)."""
        users_by_id: dict[UUID, UserModel] = {}

        ops_role = (
            self.db.query(PayflowRoleModel)
            .filter(PayflowRoleModel.code == PayflowRoleCode.OPERATIONS_ADMIN.value)
            .first()
        )
        if ops_role:
            for m in (
                self.db.query(PayflowUserMembershipModel)
                .options(joinedload(PayflowUserMembershipModel.user))
                .filter(PayflowUserMembershipModel.payflow_role_id == ops_role.id)
                .all()
            ):
                if m.user:
                    users_by_id[m.user.id] = m.user

        if client_id is not None:
            for a in (
                self.db.query(PayflowUserClientAssignmentModel)
                .options(
                    joinedload(PayflowUserClientAssignmentModel.membership).joinedload(
                        PayflowUserMembershipModel.user
                    ),
                    joinedload(PayflowUserClientAssignmentModel.membership).joinedload(
                        PayflowUserMembershipModel.role
                    ),
                )
                .filter(PayflowUserClientAssignmentModel.client_id == client_id)
                .all()
            ):
                membership = a.membership
                if not membership or not membership.user or not membership.role:
                    continue
                if membership.role.code != PayflowRoleCode.SUPERVISOR.value:
                    continue
                users_by_id[membership.user.id] = membership.user

        return list(users_by_id.values())

    def recipients_ops_admins(self) -> list[UserModel]:
        return self.recipients_for_client(None)

    # ------------------------------------------------------------------ notify core
    def notify(
        self,
        *,
        users: list[UserModel],
        notification_type: str,
        title: str,
        body: str | None = None,
        link: str | None = None,
        client_id: int | None = None,
        entity_type: str | None = None,
        entity_id: str | None = None,
        send_mail: bool = False,
        email_type: str | None = None,
        actor_user_id: UUID | None = None,
        dedupe: bool = True,
    ) -> list[PayflowNotificationModel]:
        created: list[PayflowNotificationModel] = []
        mail_targets: list[UserModel] = []
        for user in users:
            if actor_user_id and user.id == actor_user_id:
                continue
            # Dedupe across read + unread. Inbox list syncs awaiting entities on
            # every poll; requiring unread-only caused mark-read → recreate spam.
            if dedupe and entity_type and entity_id:
                exists = (
                    self.db.query(PayflowNotificationModel)
                    .filter(
                        PayflowNotificationModel.user_id == user.id,
                        PayflowNotificationModel.notification_type == notification_type,
                        PayflowNotificationModel.entity_type == entity_type,
                        PayflowNotificationModel.entity_id == str(entity_id),
                    )
                    .first()
                )
                if exists:
                    continue
            row = PayflowNotificationModel(
                user_id=user.id,
                notification_type=notification_type,
                title=title,
                body=body,
                link=link,
                client_id=client_id,
                entity_type=entity_type,
                entity_id=str(entity_id) if entity_id is not None else None,
            )
            self.db.add(row)
            created.append(row)
            if send_mail and user.email:
                mail_targets.append(user)
        if not created and not mail_targets:
            return []
        self.db.commit()
        for row in created:
            self.db.refresh(row)
        if send_mail:
            for user in mail_targets:
                send_email(
                    self.db,
                    to_email=user.email,
                    subject=title,
                    body=f"{body or title}\n\nOpen in PayFlow: {link or '/'}",
                    email_type=email_type or notification_type,
                    action_link=link,
                    related_user_id=user.id,
                )
        return created

    # ------------------------------------------------------------------ domain helpers
    def notify_review_awaiting(
        self,
        review: PayflowHumanReviewModel,
        *,
        actor_user_id: UUID | None = None,
        send_mail: bool = False,
        dedupe: bool = True,
    ) -> list[PayflowNotificationModel]:
        if review.status != PayflowReviewStatus.AWAITING_REVIEW.value:
            return []
        customer = review.account.customer_name if review.account else "Customer"
        title = f"{customer} · {review.reason}"
        rule_name = review.rule.name if review.rule else "Governance rule"
        body = f"{rule_name} · awaiting decision"
        link = f"/payflow/review/{review.id}"
        users = self.recipients_for_client(review.client_id)
        return self.notify(
            users=users,
            notification_type=PayflowNotificationType.HUMAN_REVIEW_AWAITING.value,
            title=title,
            body=body,
            link=link,
            client_id=review.client_id,
            entity_type="human_review",
            entity_id=str(review.id),
            send_mail=send_mail,
            email_type="human_review_awaiting",
            actor_user_id=actor_user_id,
            dedupe=dedupe,
        )

    def notify_workflow_awaiting(
        self,
        *,
        strategy_id: int,
        strategy_name: str,
        client_id: int,
        actor_user_id: UUID | None = None,
        send_mail: bool = False,
    ) -> list[PayflowNotificationModel]:
        users = self.recipients_for_client(client_id)
        return self.notify(
            users=users,
            notification_type=PayflowNotificationType.WORKFLOW_AWAITING_APPROVAL.value,
            title=f"Workflow needs review · {strategy_name}",
            body="A collection strategy is awaiting approval.",
            link=f"/payflow/workflows/{strategy_id}",
            client_id=client_id,
            entity_type="workflow",
            entity_id=str(strategy_id),
            send_mail=send_mail,
            email_type="workflow_awaiting_approval",
            actor_user_id=actor_user_id,
        )

    def notify_comm_awaiting_governance(
        self,
        *,
        communication_id: int,
        customer_name: str,
        client_id: int,
        channel: str,
        purpose: str,
        send_mail: bool = False,
    ) -> list[PayflowNotificationModel]:
        users = self.recipients_for_client(client_id)
        return self.notify(
            users=users,
            notification_type=PayflowNotificationType.COMM_AWAITING_GOVERNANCE.value,
            title=f"{customer_name} · {channel} awaiting governance",
            body=purpose,
            link=f"/payflow/comms/{communication_id}",
            client_id=client_id,
            entity_type="communication",
            entity_id=str(communication_id),
            send_mail=send_mail,
            email_type="comm_awaiting_governance",
        )

    def notify_integration_failed(
        self,
        *,
        integration_id: str,
        client_name: str,
        detail: str | None = None,
        send_mail: bool = True,
    ) -> list[PayflowNotificationModel]:
        users = self.recipients_ops_admins()
        return self.notify(
            users=users,
            notification_type=PayflowNotificationType.INTEGRATION_CONNECTION_FAILED.value,
            title=f"Integration connection failed · {client_name}",
            body=detail or "CRM connection test failed.",
            link=f"/payflow/integrations/{integration_id}",
            client_id=None,
            entity_type="integration",
            entity_id=integration_id,
            send_mail=send_mail,
            email_type="integration_connection_failed",
        )

    def notify_supervisor_assignment(
        self,
        *,
        user: UserModel,
        client_id: int,
        client_name: str,
        send_mail: bool = True,
    ) -> list[PayflowNotificationModel]:
        return self.notify(
            users=[user],
            notification_type=PayflowNotificationType.SUPERVISOR_ASSIGNMENT.value,
            title=f"Assigned to client · {client_name}",
            body="You can now work this client in PayFlow.",
            link=f"/payflow/clients/{client_id}",
            client_id=client_id,
            entity_type="client_assignment",
            entity_id=f"{client_id}:{user.id}",
            send_mail=send_mail,
            email_type="supervisor_assignment",
        )

    def notify_client_activated(
        self,
        *,
        client_id: int,
        client_name: str,
        actor_user_id: UUID | None = None,
        send_mail: bool = False,
    ) -> list[PayflowNotificationModel]:
        users = self.recipients_for_client(client_id)
        return self.notify(
            users=users,
            notification_type=PayflowNotificationType.CLIENT_ACTIVATED.value,
            title=f"Client activated · {client_name}",
            body="Client is live. Workflow suggestion jobs may be running.",
            link=f"/payflow/clients/{client_id}",
            client_id=client_id,
            entity_type="client",
            entity_id=str(client_id),
            send_mail=send_mail,
            email_type="client_activated",
            actor_user_id=actor_user_id,
        )

    def notify_comm_sent(
        self,
        *,
        client_id: int,
        account_id: int,
        customer_name: str,
        account_reference: str,
        channel: str,
        recipient: str | None,
        communication_id: int | None,
        communication_code: str | None,
        send_mail: bool = False,
    ) -> list[PayflowNotificationModel]:
        """In-app: collection email/SMS reached (or was logged for) the customer."""
        users = self.recipients_for_client(client_id)
        code = communication_code or (str(communication_id) if communication_id else "comm")
        return self.notify(
            users=users,
            notification_type=PayflowNotificationType.COMM_SENT.value,
            title=f"{channel} sent · {customer_name}",
            body=f"To {recipient or '—'} · {account_reference} · {code}",
            link=(
                f"/payflow/comms/{communication_id}"
                if communication_id
                else f"/payflow/cases/{account_id}"
            ),
            client_id=client_id,
            entity_type="communication",
            entity_id=code,
            send_mail=send_mail,
            email_type="comm_sent",
        )

    def notify_payment_received(
        self,
        *,
        client_id: int,
        account_id: int,
        customer_name: str,
        account_reference: str,
        amount: float,
        currency_code: str,
        attempt_public_id: str,
        option_type: str | None = None,
        resolved: bool = False,
        send_mail: bool = False,
    ) -> list[PayflowNotificationModel]:
        """In-app when Stripe/mock confirms a customer payment (backend-verified only)."""
        users = self.recipients_for_client(client_id)
        money = f"{float(amount):,.2f} {currency_code or 'USD'}"
        option = (option_type or "payment").replace("_", " ")
        body = f"{money} · {option}"
        if resolved:
            body = f"{body} · case resolved"
        return self.notify(
            users=users,
            notification_type=PayflowNotificationType.PAYMENT_RECEIVED.value,
            title=f"Payment received · {customer_name}",
            body=f"{account_reference} · {body}",
            link=f"/payflow/cases/{account_id}",
            client_id=client_id,
            entity_type="payment_attempt",
            entity_id=attempt_public_id,
            send_mail=send_mail,
            email_type="payment_received",
            dedupe=True,
        )

    def notify_payment_failed(
        self,
        *,
        client_id: int,
        account_id: int,
        customer_name: str,
        account_reference: str,
        attempt_public_id: str,
        detail: str | None = None,
        send_mail: bool = False,
    ) -> list[PayflowNotificationModel]:
        """In-app payment failure (alongside operational exception when raised)."""
        users = self.recipients_for_client(client_id)
        return self.notify(
            users=users,
            notification_type=PayflowNotificationType.PAYMENT_FAILED.value,
            title=f"Payment failed · {customer_name}",
            body=f"{account_reference} · {(detail or 'Checkout or provider failure')[:200]}",
            link=f"/payflow/cases/{account_id}",
            client_id=client_id,
            entity_type="payment_attempt",
            entity_id=f"fail:{attempt_public_id}",
            send_mail=send_mail,
            email_type="payment_failed",
            dedupe=True,
        )

    def sync_awaiting_review_notifications(self) -> int:
        """Idempotent: ensure inbox rows exist for every awaiting review."""
        rows = (
            self.db.query(PayflowHumanReviewModel)
            .options(
                joinedload(PayflowHumanReviewModel.account),
                joinedload(PayflowHumanReviewModel.rule),
            )
            .filter(PayflowHumanReviewModel.status == PayflowReviewStatus.AWAITING_REVIEW.value)
            .all()
        )
        total = 0
        for review in rows:
            created = self.notify_review_awaiting(review, send_mail=False)
            total += len(created)
        return total

    # ------------------------------------------------------------------ inbox API
    def list_for_user(self, user: UserModel, *, limit: int = 100) -> dict[str, Any]:
        self.access.require_membership(user)
        # Keep demo / existing awaiting items visible without reseed.
        self.sync_awaiting_review_notifications()
        self.sync_awaiting_workflow_notifications()
        self.sync_awaiting_comm_notifications()

        q = self.db.query(PayflowNotificationModel).filter(
            PayflowNotificationModel.user_id == user.id
        )
        unread = q.filter(PayflowNotificationModel.read_at.is_(None)).count()
        # Prefer unread first so the inbox badge matches what the dropdown shows.
        rows = (
            q.order_by(
                PayflowNotificationModel.read_at.isnot(None).asc(),
                PayflowNotificationModel.created_at.desc(),
            )
            .limit(limit)
            .all()
        )
        return {
            "notifications": [self._serialize(r) for r in rows],
            "unread_count": unread,
        }

    def mark_read(self, user: UserModel, notification_id: int) -> dict[str, Any]:
        row = (
            self.db.query(PayflowNotificationModel)
            .filter(
                PayflowNotificationModel.id == notification_id,
                PayflowNotificationModel.user_id == user.id,
            )
            .first()
        )
        if not row:
            raise NotFoundError("Notification not found")
        if row.read_at is None:
            row.read_at = datetime.now(timezone.utc)
            self.db.commit()
            self.db.refresh(row)
        return self._serialize(row)

    def mark_all_read(self, user: UserModel) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        (
            self.db.query(PayflowNotificationModel)
            .filter(
                PayflowNotificationModel.user_id == user.id,
                PayflowNotificationModel.read_at.is_(None),
            )
            .update({PayflowNotificationModel.read_at: now}, synchronize_session=False)
        )
        self.db.commit()
        return self.list_for_user(user)

    def mark_entity_read(self, *, entity_type: str, entity_id: str | int) -> None:
        now = datetime.now(timezone.utc)
        (
            self.db.query(PayflowNotificationModel)
            .filter(
                PayflowNotificationModel.entity_type == entity_type,
                PayflowNotificationModel.entity_id == str(entity_id),
                PayflowNotificationModel.read_at.is_(None),
            )
            .update({PayflowNotificationModel.read_at: now}, synchronize_session=False)
        )
        self.db.commit()

    def sync_awaiting_workflow_notifications(self) -> int:
        from app.infrastructure.database.models import PayflowStrategyModel
        from app.shared.enums import PayflowStrategyStatus

        rows = (
            self.db.query(PayflowStrategyModel)
            .filter(
                PayflowStrategyModel.status.in_(
                    [
                        PayflowStrategyStatus.AI_PROPOSED.value,
                        PayflowStrategyStatus.UNDER_REVIEW.value,
                    ]
                )
            )
            .all()
        )
        total = 0
        for row in rows:
            created = self.notify_workflow_awaiting(
                strategy_id=row.id,
                strategy_name=row.name,
                client_id=row.client_id,
                send_mail=False,
            )
            total += len(created)
        return total

    def sync_awaiting_comm_notifications(self) -> int:
        from app.infrastructure.database.models import PayflowCommunicationModel
        from app.shared.enums import PayflowCommStatus

        rows = (
            self.db.query(PayflowCommunicationModel)
            .options(joinedload(PayflowCommunicationModel.account))
            .filter(
                PayflowCommunicationModel.status == PayflowCommStatus.AWAITING_GOVERNANCE.value
            )
            .all()
        )
        total = 0
        for row in rows:
            customer = row.account.customer_name if row.account else "Customer"
            created = self.notify_comm_awaiting_governance(
                communication_id=row.id,
                customer_name=customer,
                client_id=row.client_id,
                channel=row.channel,
                purpose=row.purpose,
                send_mail=False,
            )
            total += len(created)
        return total

    def _serialize(self, row: PayflowNotificationModel) -> dict[str, Any]:
        return {
            "id": row.id,
            "notification_type": row.notification_type,
            "title": row.title,
            "body": row.body,
            "link": row.link,
            "client_id": row.client_id,
            "entity_type": row.entity_type,
            "entity_id": row.entity_id,
            "read": row.read_at is not None,
            "read_at": row.read_at,
            "created_at": row.created_at,
        }
