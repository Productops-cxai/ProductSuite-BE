from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, File, HTTPException, Query, UploadFile, status
from fastapi.responses import Response

from app.core.deps import DbSession
from app.core.exceptions import AppError, NotFoundError
from app.modules.payflow.deps import (
    PayflowAccessContext,
    PayflowCreateClient,
    PayflowDeleteClient,
    PayflowDeleteRules,
    PayflowDeleteWorkflows,
    PayflowEditClient,
    PayflowImportAccounts,
    PayflowImportClients,
    PayflowManageIntegrations,
    PayflowManageUsers,
    PayflowManageWorkflows,
    PayflowOpsAdmin,
    PayflowViewImports,
    RequirePayflowProduct,
)
from app.modules.payflow.schemas import (
    AccessContextResponse,
    ApproveReviewRequest,
    ActivateStrategyRequest,
    ApproveStrategyRequest,
    AssignReviewRequest,
    CreateHumanReviewRequest,
    CreatePayflowClientRequest,
    CreatePayflowRoleRequest,
    CreatePayflowRuleRequest,
    CreatePayflowStrategyRequest,
    CreatePayflowUserRequest,
    CreatePortfolioRequest,
    HoldReviewRequest,
    IntegrationTestResponse,
    MessageResponse,
    MenusResponse,
    ModifyReviewRequest,
    PayflowAccountsListResponse,
    PayflowClientsListResponse,
    CommunicationEligibilitySnapshot,
    ManualSmsSendRequest,
    ManualSmsSendResponse,
    PayflowCommunicationItem,
    PayflowCommunicationsListResponse,
    PayflowOperationalExceptionItem,
    PayflowOperationalExceptionsListResponse,
    ResolveOperationalExceptionRequest,
    PayflowIntegrationItem,
    PayflowIntegrationsListResponse,
    PayflowNotificationItem,
    PayflowNotificationsListResponse,
    PayflowPermissionsCatalogResponse,
    PayflowReviewItem,
    PayflowReviewsListResponse,
    PayflowRolesListResponse,
    PayflowRuleItem,
    PayflowRulesListResponse,
    PayflowStrategiesListResponse,
    PayflowStrategyItem,
    PayflowUserDetailResponse,
    PayflowUsersListResponse,
    RejectReviewRequest,
    RejectStrategyRequest,
    ReleaseReviewRequest,
    UpdatePayflowClientRequest,
    UpdatePayflowRoleRequest,
    UpdatePayflowStrategyRequest,
    UpdatePayflowUserRequest,
    UpdatePortfolioRequest,
    PayflowDashboardResponse,
    PayflowImportListResponse,
    PayflowImportPreviewResponse,
    PayflowImportRunItem,
    PortfolioSegmentsResponse,
    SuggestWorkflowsRequest,
    SuggestWorkflowsResponse,
    RegenerateWorkflowRequest,
    PayflowWorkflowPaymentEventRequest,
    PayflowWorkflowRunItem,
    PayflowWorkflowRunsResponse,
    PayflowWorkflowWorkerLogsResponse,
)
from app.modules.platform.schemas import DeletionLogResponse
from app.modules.payflow.service import PayflowUserService
from app.modules.payflow.services.access_context_service import AccessContextService
from app.modules.payflow.services.account_service import PayflowAccountService
from app.modules.payflow.services.client_service import PayflowClientService
from app.modules.payflow.services.communication_service import PayflowCommunicationService
from app.modules.payflow.services.dashboard_service import PayflowDashboardService
from app.modules.payflow.services.import_service import PayflowImportService
from app.modules.payflow.services.integration_service import PayflowIntegrationService
from app.modules.payflow.services.notification_service import PayflowNotificationService
from app.modules.payflow.services.operational_exception_service import (
    PayflowOperationalExceptionService,
)
from app.modules.payflow.services.review_service import PayflowReviewService
from app.modules.payflow.services.rule_service import PayflowRuleService
from app.modules.payflow.services.geo_service import PayflowGeoService
from app.modules.payflow.services.strategy_service import PayflowStrategyService
from app.modules.payflow.services.ai_suggest_job_service import schedule_ai_suggest_jobs
from app.modules.payflow.services.workflow_suggestion.service import WorkflowSuggestionService

# Default tag is overridden in app.core.openapi_tags for Swagger sections.
router = APIRouter(prefix="/payflow", tags=["PayFlow · Access"])


def _map_error(exc: AppError) -> HTTPException:
    code_map = {
        "unauthorized": status.HTTP_401_UNAUTHORIZED,
        "forbidden": status.HTTP_403_FORBIDDEN,
        "not_found": status.HTTP_404_NOT_FOUND,
        "conflict": status.HTTP_409_CONFLICT,
        "validation_error": status.HTTP_400_BAD_REQUEST,
        "service_unavailable": status.HTTP_502_BAD_GATEWAY,
    }
    return HTTPException(status_code=code_map.get(exc.code, 400), detail=exc.message)


@router.get("/health")
def payflow_health(user: RequirePayflowProduct, ctx: PayflowAccessContext):
    return {
        "status": "ok",
        "product": "PAYFLOW",
        "user_id": str(user.id),
        "role": ctx["role"]["code"],
        "is_operations_admin": ctx["is_operations_admin"],
    }


@router.get("/geo/countries")
def geo_countries(_: RequirePayflowProduct):
    try:
        return PayflowGeoService().list_countries()
    except AppError as exc:
        raise _map_error(exc) from exc


@router.get("/geo/states")
def geo_states(_: RequirePayflowProduct, country: str = Query(min_length=1)):
    try:
        return PayflowGeoService().list_states(country)
    except AppError as exc:
        raise _map_error(exc) from exc


@router.get("/geo/cities")
def geo_cities(
    _: RequirePayflowProduct,
    country: str = Query(min_length=1),
    state: str = Query(min_length=1),
):
    try:
        return PayflowGeoService().list_cities(country, state)
    except AppError as exc:
        raise _map_error(exc) from exc


@router.get("/access-context", response_model=AccessContextResponse)
def get_access_context(ctx: PayflowAccessContext):
    return ctx


@router.get("/menus", response_model=MenusResponse)
def get_payflow_menus(user: RequirePayflowProduct, db: DbSession):
    try:
        return AccessContextService(db).menus_for_user(user)
    except AppError as exc:
        raise _map_error(exc) from exc


@router.get("/permissions", response_model=PayflowPermissionsCatalogResponse)
def list_permissions(_: PayflowManageUsers, db: DbSession):
    return PayflowUserService(db).list_permissions_catalog()


@router.get("/roles", response_model=PayflowRolesListResponse)
def list_roles(_: PayflowManageUsers, db: DbSession):
    return PayflowUserService(db).list_roles()


@router.post("/roles", response_model=PayflowRolesListResponse)
def create_role(payload: CreatePayflowRoleRequest, admin: PayflowOpsAdmin, db: DbSession):
    """Only Operations Admin may create roles."""
    try:
        return PayflowUserService(db).create_role(
            actor=admin,
            name=payload.name,
            scope=payload.scope,
            description=payload.description,
            permission_codes=payload.permission_codes,
        )
    except AppError as exc:
        raise _map_error(exc) from exc


@router.post("/roles/{role_id}/update", response_model=PayflowRolesListResponse)
def update_role(
    role_id: int,
    payload: UpdatePayflowRoleRequest,
    admin: PayflowOpsAdmin,
    db: DbSession,
):
    """Only Operations Admin may edit roles / permissions."""
    try:
        return PayflowUserService(db).update_role(
            role_id,
            actor=admin,
            name=payload.name,
            description=payload.description,
            permission_codes=payload.permission_codes,
        )
    except AppError as exc:
        raise _map_error(exc) from exc


@router.post("/roles/{role_id}/delete", response_model=MessageResponse)
def delete_role(
    role_id: int,
    admin: PayflowOpsAdmin,
    db: DbSession,
    source: str | None = Query(default=None),
):
    """Only Operations Admin may delete roles."""
    try:
        return PayflowUserService(db).delete_role(role_id, actor=admin, source=source)
    except AppError as exc:
        raise _map_error(exc) from exc


# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------


@router.get("/clients/mapping-catalog")
def get_mapping_catalog(user: RequirePayflowProduct, db: DbSession):
    # Read-only catalog — available to any PayFlow user (supervisor config view).
    _ = user
    return PayflowClientService(db).mapping_catalog()


@router.get("/clients/bulk-template")
def download_bulk_template(_: PayflowImportClients, db: DbSession):
    content = PayflowClientService(db).bulk_template_bytes()
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": 'attachment; filename="payflow_clients_bulk_template.xlsx"'
        },
    )


@router.post("/clients/bulk-validate", response_model=PayflowImportPreviewResponse)
async def bulk_validate_clients(
    _: PayflowImportClients,
    db: DbSession,
    file: UploadFile = File(...),
):
    name = (file.filename or "").lower()
    if not name.endswith((".xlsx", ".xlsm", ".csv")):
        raise HTTPException(status_code=400, detail="Upload an .xlsx or .csv file")
    data = await file.read()
    return PayflowClientService(db).bulk_validate(data, file.filename or "upload.xlsx")


@router.post("/clients/bulk-upload")
async def bulk_upload_clients(
    user: PayflowImportClients,
    db: DbSession,
    file: UploadFile = File(...),
):
    name = (file.filename or "").lower()
    if not name.endswith((".xlsx", ".xlsm", ".csv")):
        raise HTTPException(status_code=400, detail="Upload an .xlsx or .csv file")
    data = await file.read()
    try:
        return PayflowClientService(db).bulk_upload(
            data, user=user, filename=file.filename or "upload.xlsx"
        )
    except AppError as exc:
        raise _map_error(exc) from exc


@router.get("/clients", response_model=PayflowClientsListResponse)
def list_clients(
    user: RequirePayflowProduct,
    db: DbSession,
    search: str | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    client_type: str | None = Query(default=None),
    business_domain: str | None = Query(default=None),
    ai_mode: str | None = Query(default=None),
    supervisor_user_id: str | None = Query(default=None),
):
    try:
        access = AccessContextService(db)
        allowed_ids = None
        if not access.is_operations_admin(user):
            ctx = access.build_context(user)
            allowed_ids = [
                cid
                for cid in (ctx.get("client_ids") or [])
                if access.can(user, "view_client", cid)
            ]
        return PayflowClientService(db).list_clients(
            search=search,
            status=status_filter,
            client_type=client_type,
            business_domain=business_domain,
            ai_mode=ai_mode,
            supervisor_user_id=supervisor_user_id,
            client_ids=allowed_ids,
        )
    except AppError as exc:
        raise _map_error(exc) from exc


@router.post("/clients")
def create_client(payload: CreatePayflowClientRequest, _: PayflowCreateClient, db: DbSession):
    try:
        data = payload.model_dump()
        # Wizard creates are always "Add Client" (bulk import sets file_upload itself).
        data["added_through"] = "add_client"
        return PayflowClientService(db).create_client(data)
    except AppError as exc:
        raise _map_error(exc) from exc


@router.get("/clients/{client_id}")
def get_client(client_id: int, user: RequirePayflowProduct, db: DbSession):
    try:
        access = AccessContextService(db)
        if not access.can_see_client(user, client_id):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Client access denied")
        return PayflowClientService(db).get_client(client_id)
    except AppError as exc:
        raise _map_error(exc) from exc


@router.post("/clients/{client_id}/update")
def update_client(
    client_id: int,
    payload: UpdatePayflowClientRequest,
    _: PayflowEditClient,
    db: DbSession,
):
    try:
        data = payload.model_dump(exclude_unset=True)
        if "supervisor_user_ids" in data and data["supervisor_user_ids"] is not None:
            data["supervisor_user_ids"] = [str(u) for u in data["supervisor_user_ids"]]
        return PayflowClientService(db).update_client(client_id, data)
    except AppError as exc:
        raise _map_error(exc) from exc


@router.post("/clients/{client_id}/logo")
async def upload_client_logo(
    client_id: int,
    _: PayflowEditClient,
    db: DbSession,
    file: UploadFile = File(...),
):
    try:
        data = await file.read()
        return PayflowClientService(db).update_logo(
            client_id,
            file_bytes=data,
            content_type=file.content_type,
            filename=file.filename,
        )
    except AppError as exc:
        raise _map_error(exc) from exc


@router.post("/clients/{client_id}/logo/delete")
def delete_client_logo(client_id: int, _: PayflowEditClient, db: DbSession):
    try:
        return PayflowClientService(db).remove_logo(client_id)
    except AppError as exc:
        raise _map_error(exc) from exc


@router.post("/clients/{client_id}/activate")
def activate_client(
    client_id: int,
    admin: PayflowEditClient,
    db: DbSession,
    background_tasks: BackgroundTasks,
):
    try:
        detail, job_ids = PayflowClientService(db).activate_client(client_id, user=admin)
        schedule_ai_suggest_jobs(background_tasks, job_ids)
        return detail
    except AppError as exc:
        raise _map_error(exc) from exc


@router.get("/clients/{client_id}/portfolios")
def list_portfolios(client_id: int, user: RequirePayflowProduct, db: DbSession):
    try:
        access = AccessContextService(db)
        if not access.can_see_client(user, client_id):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Client access denied")
        return PayflowClientService(db).list_portfolios(client_id)
    except AppError as exc:
        raise _map_error(exc) from exc


@router.get("/clients/{client_id}/portfolios/{portfolio_id}")
def get_portfolio(client_id: int, portfolio_id: int, user: RequirePayflowProduct, db: DbSession):
    try:
        access = AccessContextService(db)
        if not access.can_see_client(user, client_id):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Client access denied")
        return PayflowClientService(db).get_portfolio(client_id, portfolio_id)
    except AppError as exc:
        raise _map_error(exc) from exc


@router.post("/clients/{client_id}/portfolios")
def create_portfolio(
    client_id: int,
    payload: CreatePortfolioRequest,
    _: PayflowEditClient,
    db: DbSession,
):
    try:
        return PayflowClientService(db).create_portfolio(client_id, payload.model_dump())
    except AppError as exc:
        raise _map_error(exc) from exc


@router.post("/clients/{client_id}/portfolios/{portfolio_id}/update")
def update_portfolio(
    client_id: int,
    portfolio_id: int,
    payload: UpdatePortfolioRequest,
    admin: PayflowEditClient,
    db: DbSession,
    background_tasks: BackgroundTasks,
):
    try:
        detail, job_ids = PayflowClientService(db).update_portfolio(
            client_id,
            portfolio_id,
            payload.model_dump(exclude_unset=True),
            user=admin,
        )
        schedule_ai_suggest_jobs(background_tasks, job_ids)
        return detail
    except AppError as exc:
        raise _map_error(exc) from exc


@router.post("/clients/{client_id}/delete", response_model=MessageResponse)
def delete_client(
    client_id: int,
    admin: PayflowDeleteClient,
    db: DbSession,
    source: str | None = Query(default=None),
):
    try:
        return PayflowClientService(db).delete_client(client_id, actor=admin, source=source)
    except AppError as exc:
        raise _map_error(exc) from exc


@router.post("/clients/{client_id}/portfolios/{portfolio_id}/delete", response_model=MessageResponse)
def delete_portfolio(
    client_id: int,
    portfolio_id: int,
    admin: PayflowDeleteClient,
    db: DbSession,
    source: str | None = Query(default=None),
):
    try:
        return PayflowClientService(db).delete_portfolio(
            client_id, portfolio_id, actor=admin, source=source
        )
    except AppError as exc:
        raise _map_error(exc) from exc


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


@router.get("/dashboard", response_model=PayflowDashboardResponse)
def get_dashboard(
    user: RequirePayflowProduct,
    db: DbSession,
    date_range: str = Query(default="today"),
    client_id: int | None = Query(default=None),
    channel: str | None = Query(default=None),
    workflow: str | None = Query(default=None),
):
    try:
        return PayflowDashboardService(db).get_dashboard(
            user,
            date_range=date_range,
            client_id=client_id,
            channel=channel,
            workflow=workflow,
        )
    except AppError as exc:
        raise _map_error(exc) from exc


# ---------------------------------------------------------------------------
# Accounts / Cases
# ---------------------------------------------------------------------------


@router.get("/accounts", response_model=PayflowAccountsListResponse)
def list_accounts(
    user: RequirePayflowProduct,
    db: DbSession,
    client_id: int | None = Query(default=None),
    portfolio_id: int | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    workflow: str | None = Query(default=None),
    human_review: str | None = Query(default=None),
    search: str | None = Query(default=None),
):
    try:
        return PayflowAccountService(db).list_accounts(
            user,
            client_id=client_id,
            portfolio_id=portfolio_id,
            status=status_filter,
            workflow=workflow,
            human_review=human_review,
            search=search,
        )
    except AppError as exc:
        raise _map_error(exc) from exc


@router.get("/accounts/{account_id}")
def get_account(account_id: int, user: RequirePayflowProduct, db: DbSession):
    try:
        return PayflowAccountService(db).get_account(user, account_id)
    except AppError as exc:
        raise _map_error(exc) from exc


@router.get("/accounts/{account_id}/workflow-execution")
def get_account_workflow_execution(
    account_id: int, user: RequirePayflowProduct, db: DbSession
):
    """Active (or latest) workflow run for a Collection Case, enriched for the Case UI."""
    from app.modules.payflow.services.workflow_execution_service import WorkflowExecutionService

    # Enforce account access via existing account service.
    try:
        PayflowAccountService(db).get_account(user, account_id)
    except AppError as exc:
        raise _map_error(exc) from exc
    item = WorkflowExecutionService(db).get_account_workflow_execution(account_id)
    return {"execution": item}


@router.post("/accounts/{account_id}/workflow-events/payment")
def post_account_payment_workflow_event(
    account_id: int,
    body: PayflowWorkflowPaymentEventRequest,
    user: RequirePayflowProduct,
    db: DbSession,
):
    from app.modules.payflow.services.workflow_execution_service import WorkflowExecutionService

    try:
        PayflowAccountService(db).get_account(user, account_id)
    except AppError as exc:
        raise _map_error(exc) from exc
    result = WorkflowExecutionService(db).on_payment_event(
        account_id,
        body.model_dump(exclude_none=True),
        commit=True,
    )
    return {"ok": True, **result}


# ---------------------------------------------------------------------------
# Daily CRM account import
# ---------------------------------------------------------------------------


@router.get("/imports/accounts/template")
def download_account_import_template(_: PayflowImportAccounts, db: DbSession):
    content = PayflowImportService(db).template_bytes(include_samples=True)
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": 'attachment; filename="payflow_daily_accounts_sample.xlsx"'
        },
    )


@router.post("/imports/accounts/validate", response_model=PayflowImportPreviewResponse)
async def validate_account_import(
    _: PayflowImportAccounts,
    db: DbSession,
    file: UploadFile = File(...),
):
    if not file.filename or not file.filename.lower().endswith((".xlsx", ".xlsm", ".csv")):
        raise HTTPException(status_code=400, detail="Upload an .xlsx or .csv file")
    data = await file.read()
    return PayflowImportService(db).validate_account_file(data, file.filename or "upload.xlsx")


@router.post("/imports/accounts/upload", response_model=PayflowImportRunItem)
async def upload_account_import(
    admin: PayflowImportAccounts,
    db: DbSession,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
):
    name = (file.filename or "").lower()
    if not name.endswith((".xlsx", ".xlsm", ".csv")):
        raise HTTPException(status_code=400, detail="Upload an .xlsx or .csv file")
    data = await file.read()
    try:
        result, job_ids = PayflowImportService(db).process_account_file(
            admin, data, file.filename or "upload.xlsx"
        )
        schedule_ai_suggest_jobs(background_tasks, job_ids)
        return result
    except AppError as exc:
        raise _map_error(exc) from exc


@router.get("/imports", response_model=PayflowImportListResponse)
def list_imports(
    _: PayflowViewImports,
    db: DbSession,
    type_filter: str | None = Query(default=None, alias="type"),
):
    return PayflowImportService(db).list_imports(kind=type_filter)


@router.get("/imports/{import_id}", response_model=PayflowImportRunItem)
def get_import(import_id: int, _: PayflowViewImports, db: DbSession):
    try:
        return PayflowImportService(db).get_import(import_id)
    except AppError as exc:
        raise _map_error(exc) from exc


# ---------------------------------------------------------------------------
# Integrations (derived from client configuration)
# ---------------------------------------------------------------------------


@router.get("/integrations", response_model=PayflowIntegrationsListResponse)
def list_integrations(
    user: PayflowManageIntegrations,
    db: DbSession,
    status_filter: str | None = Query(default=None, alias="status"),
    client_id: int | None = Query(default=None),
    category: str | None = Query(default=None),
):
    try:
        return PayflowIntegrationService(db).list_integrations(
            user,
            status=status_filter,
            client_id=client_id,
            category=category,
        )
    except AppError as exc:
        raise _map_error(exc) from exc


@router.get("/integrations/{integration_id}", response_model=PayflowIntegrationItem)
def get_integration(integration_id: str, user: PayflowManageIntegrations, db: DbSession):
    try:
        return PayflowIntegrationService(db).get_integration(user, integration_id)
    except AppError as exc:
        raise _map_error(exc) from exc


@router.post("/integrations/{integration_id}/test", response_model=IntegrationTestResponse)
def test_integration(integration_id: str, user: PayflowManageIntegrations, db: DbSession):
    try:
        return PayflowIntegrationService(db).test_connection(user, integration_id)
    except AppError as exc:
        raise _map_error(exc) from exc


# ---------------------------------------------------------------------------
# Human Review
# ---------------------------------------------------------------------------


@router.get("/reviews", response_model=PayflowReviewsListResponse)
def list_reviews(
    user: RequirePayflowProduct,
    db: DbSession,
    client_id: int | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    priority: str | None = Query(default=None),
    reason: str | None = Query(default=None),
    rule_id: int | None = Query(default=None),
    proposed_action: str | None = Query(default=None),
    search: str | None = Query(default=None),
    waiting_bucket: str | None = Query(default=None),
    sort_by: str = Query(default="waiting_minutes"),
    sort_dir: str = Query(default="desc"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
):
    try:
        return PayflowReviewService(db).list_reviews(
            user,
            client_id=client_id,
            status=status_filter,
            priority=priority,
            reason=reason,
            rule_id=rule_id,
            proposed_action=proposed_action,
            search=search,
            waiting_bucket=waiting_bucket,
            sort_by=sort_by,
            sort_dir=sort_dir,
            page=page,
            page_size=page_size,
        )
    except AppError as exc:
        raise _map_error(exc) from exc


@router.post("/reviews", response_model=PayflowReviewItem)
def create_review(
    payload: CreateHumanReviewRequest,
    user: RequirePayflowProduct,
    db: DbSession,
):
    try:
        return PayflowReviewService(db).create_review_for_user(
            user, payload.model_dump()
        )
    except AppError as exc:
        raise _map_error(exc) from exc


@router.get("/reviews/{review_id}", response_model=PayflowReviewItem)
def get_review(review_id: int, user: RequirePayflowProduct, db: DbSession):
    try:
        return PayflowReviewService(db).get_review(user, review_id)
    except AppError as exc:
        raise _map_error(exc) from exc


@router.post("/reviews/{review_id}/assign", response_model=PayflowReviewItem)
def assign_review(
    review_id: int,
    payload: AssignReviewRequest,
    user: RequirePayflowProduct,
    db: DbSession,
):
    try:
        return PayflowReviewService(db).assign_review(
            user,
            review_id,
            assigned_user_id=payload.assigned_user_id,
            assigned_supervisor=payload.assigned_supervisor,
        )
    except AppError as exc:
        raise _map_error(exc) from exc


@router.post("/reviews/{review_id}/approve", response_model=PayflowReviewItem)
def approve_review(
    review_id: int,
    payload: ApproveReviewRequest,
    user: RequirePayflowProduct,
    db: DbSession,
):
    try:
        return PayflowReviewService(db).approve(
            user,
            review_id,
            note=payload.note,
            decision_version=payload.decision_version,
        )
    except AppError as exc:
        raise _map_error(exc) from exc


@router.post("/reviews/{review_id}/modify", response_model=PayflowReviewItem)
def modify_review(
    review_id: int,
    payload: ModifyReviewRequest,
    user: RequirePayflowProduct,
    db: DbSession,
):
    try:
        return PayflowReviewService(db).modify(
            user,
            review_id,
            action=payload.action,
            guidance=payload.guidance,
            decision_version=payload.decision_version,
        )
    except AppError as exc:
        raise _map_error(exc) from exc


@router.post("/reviews/{review_id}/reject", response_model=PayflowReviewItem)
def reject_review(
    review_id: int,
    payload: RejectReviewRequest,
    user: RequirePayflowProduct,
    db: DbSession,
):
    try:
        return PayflowReviewService(db).reject(
            user,
            review_id,
            reason=payload.reason,
            comment=payload.comment,
            decision_version=payload.decision_version,
        )
    except AppError as exc:
        raise _map_error(exc) from exc


@router.post("/reviews/{review_id}/hold", response_model=PayflowReviewItem)
def hold_review(
    review_id: int,
    payload: HoldReviewRequest,
    user: RequirePayflowProduct,
    db: DbSession,
):
    try:
        return PayflowReviewService(db).hold(
            user,
            review_id,
            until=payload.until,
            reason=payload.reason,
            decision_version=payload.decision_version,
        )
    except AppError as exc:
        raise _map_error(exc) from exc


@router.post("/reviews/{review_id}/release", response_model=PayflowReviewItem)
def release_review(
    review_id: int,
    payload: ReleaseReviewRequest,
    user: RequirePayflowProduct,
    db: DbSession,
):
    try:
        return PayflowReviewService(db).release(
            user,
            review_id,
            note=payload.note,
            decision_version=payload.decision_version,
        )
    except AppError as exc:
        raise _map_error(exc) from exc


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------


@router.get("/rules/catalog")
def get_rules_catalog(user: RequirePayflowProduct, db: DbSession):
    _ = user
    return PayflowRuleService(db).catalog()


@router.get("/rules", response_model=PayflowRulesListResponse)
def list_rules(
    user: RequirePayflowProduct,
    db: DbSession,
    client_id: int | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    category: str | None = Query(default=None),
    action: str | None = Query(default=None),
    rule_type: str | None = Query(default=None),
    search: str | None = Query(default=None),
):
    try:
        return PayflowRuleService(db).list_rules(
            user,
            client_id=client_id,
            status=status_filter,
            category=category,
            action=action,
            rule_type=rule_type,
            search=search,
        )
    except AppError as exc:
        raise _map_error(exc) from exc


@router.post("/rules", response_model=PayflowRuleItem)
def create_or_update_rule(
    payload: CreatePayflowRuleRequest,
    user: RequirePayflowProduct,
    db: DbSession,
):
    """Create a rule, or update when ``id`` is present in the body (same POST)."""
    try:
        body = {
            **payload.model_dump(),
            "conditions": [c.model_dump() for c in payload.conditions],
        }
        return PayflowRuleService(db).create_rule(user, body)
    except AppError as exc:
        raise _map_error(exc) from exc


@router.get("/rules/{rule_id}", response_model=PayflowRuleItem)
def get_rule(rule_id: int, user: RequirePayflowProduct, db: DbSession):
    try:
        return PayflowRuleService(db).get_rule(user, rule_id)
    except AppError as exc:
        raise _map_error(exc) from exc


@router.post("/rules/{rule_id}/activate", response_model=PayflowRuleItem)
def activate_rule(rule_id: int, user: RequirePayflowProduct, db: DbSession):
    try:
        return PayflowRuleService(db).activate(user, rule_id)
    except AppError as exc:
        raise _map_error(exc) from exc


@router.post("/rules/{rule_id}/deactivate", response_model=PayflowRuleItem)
def deactivate_rule(rule_id: int, user: RequirePayflowProduct, db: DbSession):
    try:
        return PayflowRuleService(db).deactivate(user, rule_id)
    except AppError as exc:
        raise _map_error(exc) from exc


@router.post("/rules/{rule_id}/delete", response_model=MessageResponse)
def delete_rule(
    rule_id: int,
    admin: PayflowDeleteRules,
    db: DbSession,
    source: str | None = Query(default=None),
):
    try:
        return PayflowRuleService(db).delete_rule(admin, rule_id, source=source)
    except AppError as exc:
        raise _map_error(exc) from exc


# ---------------------------------------------------------------------------
# Strategies / Workflows
# ---------------------------------------------------------------------------


@router.get("/workflows", response_model=PayflowStrategiesListResponse)
def list_workflows(
    user: RequirePayflowProduct,
    db: DbSession,
    client_id: int | None = Query(default=None),
    portfolio_id: int | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    search: str | None = Query(default=None),
):
    try:
        return PayflowStrategyService(db).list_strategies(
            user,
            client_id=client_id,
            portfolio_id=portfolio_id,
            status=status_filter,
            search=search,
        )
    except AppError as exc:
        raise _map_error(exc) from exc


@router.post("/workflows", response_model=PayflowStrategyItem)
def create_workflow(
    payload: CreatePayflowStrategyRequest,
    user: PayflowManageWorkflows,
    db: DbSession,
):
    try:
        data = payload.model_dump()
        data["steps"] = [s.model_dump(exclude_none=True) for s in payload.steps]
        if payload.ai_context is not None:
            data["ai_context"] = [c.model_dump() for c in payload.ai_context]
        return PayflowStrategyService(db).create_strategy(user, data)
    except AppError as exc:
        raise _map_error(exc) from exc


@router.get("/workflows/{strategy_id}", response_model=PayflowStrategyItem)
def get_workflow(strategy_id: int, user: RequirePayflowProduct, db: DbSession):
    try:
        return PayflowStrategyService(db).get_strategy(user, strategy_id)
    except AppError as exc:
        raise _map_error(exc) from exc


@router.post("/workflows/{strategy_id}/update", response_model=PayflowStrategyItem)
def update_workflow(
    strategy_id: int,
    payload: UpdatePayflowStrategyRequest,
    user: PayflowManageWorkflows,
    db: DbSession,
):
    try:
        data = payload.model_dump(exclude_unset=True)
        if "steps" in data and data["steps"] is not None:
            data["steps"] = [
                s.model_dump(exclude_none=True) if hasattr(s, "model_dump") else s
                for s in payload.steps or []
            ]
        return PayflowStrategyService(db).update_strategy(user, strategy_id, data)
    except AppError as exc:
        raise _map_error(exc) from exc


@router.post("/workflows/{strategy_id}/save-draft", response_model=PayflowStrategyItem)
def save_workflow_draft(strategy_id: int, user: PayflowManageWorkflows, db: DbSession):
    try:
        return PayflowStrategyService(db).save_draft(user, strategy_id)
    except AppError as exc:
        raise _map_error(exc) from exc


@router.post("/workflows/{strategy_id}/begin-review", response_model=PayflowStrategyItem)
def begin_workflow_review(strategy_id: int, user: PayflowManageWorkflows, db: DbSession):
    try:
        return PayflowStrategyService(db).begin_review(user, strategy_id)
    except AppError as exc:
        raise _map_error(exc) from exc


@router.post("/workflows/{strategy_id}/submit-review", response_model=PayflowStrategyItem)
def submit_workflow_review(strategy_id: int, user: PayflowManageWorkflows, db: DbSession):
    try:
        return PayflowStrategyService(db).submit_review(user, strategy_id)
    except AppError as exc:
        raise _map_error(exc) from exc


@router.post("/workflows/{strategy_id}/approve", response_model=PayflowStrategyItem)
def approve_workflow(
    strategy_id: int,
    user: PayflowManageWorkflows,
    db: DbSession,
    payload: ApproveStrategyRequest | None = None,  # body is optional
):
    try:
        note = payload.note if payload else None
        return PayflowStrategyService(db).approve(user, strategy_id, note=note)
    except AppError as exc:
        raise _map_error(exc) from exc


@router.post("/workflows/{strategy_id}/reject", response_model=PayflowStrategyItem)
def reject_workflow(
    strategy_id: int,
    payload: RejectStrategyRequest,
    user: PayflowManageWorkflows,
    db: DbSession,
):
    try:
        return PayflowStrategyService(db).reject(user, strategy_id, note=payload.note)
    except AppError as exc:
        raise _map_error(exc) from exc


@router.post("/workflows/{strategy_id}/activate", response_model=PayflowStrategyItem)
def activate_workflow(
    strategy_id: int,
    user: PayflowManageWorkflows,
    db: DbSession,
    payload: ActivateStrategyRequest | None = None,
):
    try:
        note = payload.note if payload else None
        return PayflowStrategyService(db).activate(user, strategy_id, note=note)
    except AppError as exc:
        raise _map_error(exc) from exc


@router.post("/workflows/{strategy_id}/deactivate", response_model=PayflowStrategyItem)
def deactivate_workflow(strategy_id: int, user: PayflowManageWorkflows, db: DbSession):
    try:
        return PayflowStrategyService(db).deactivate(user, strategy_id)
    except AppError as exc:
        raise _map_error(exc) from exc


@router.post("/workflows/{strategy_id}/delete", response_model=MessageResponse)
def delete_workflow(
    strategy_id: int,
    admin: PayflowDeleteWorkflows,
    db: DbSession,
    source: str | None = Query(default=None),
):
    try:
        return PayflowStrategyService(db).delete_strategy(admin, strategy_id, source=source)
    except AppError as exc:
        raise _map_error(exc) from exc


# ---------------------------------------------------------------------------
# Workflow suggestions (AI)
# ---------------------------------------------------------------------------


@router.get(
    "/clients/{client_id}/portfolios/{portfolio_id}/segments",
    response_model=PortfolioSegmentsResponse,
)
def preview_portfolio_segments(
    client_id: int, portfolio_id: int, user: RequirePayflowProduct, db: DbSession
):
    try:
        return WorkflowSuggestionService(db).preview_segments(user, client_id, portfolio_id)
    except AppError as exc:
        raise _map_error(exc) from exc


@router.post("/workflows/suggest", response_model=SuggestWorkflowsResponse)
def suggest_workflows(
    payload: SuggestWorkflowsRequest, user: PayflowManageWorkflows, db: DbSession
):
    try:
        return WorkflowSuggestionService(db).suggest_workflows(
            user, payload.client_id, payload.portfolio_id
        )
    except AppError as exc:
        raise _map_error(exc) from exc


@router.post("/workflows/{strategy_id}/regenerate", response_model=PayflowStrategyItem)
def regenerate_workflow(
    strategy_id: int,
    payload: RegenerateWorkflowRequest,
    user: PayflowManageWorkflows,
    db: DbSession,
):
    try:
        return WorkflowSuggestionService(db).regenerate_workflow(user, strategy_id, payload.remarks)
    except AppError as exc:
        raise _map_error(exc) from exc


# ---------------------------------------------------------------------------
# Communications
# ---------------------------------------------------------------------------


@router.get("/comms", response_model=PayflowCommunicationsListResponse)
def list_comms(
    user: RequirePayflowProduct,
    db: DbSession,
    client_id: int | None = Query(default=None),
    account_id: int | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    channel: str | None = Query(default=None),
    purpose: str | None = Query(default=None),
    workflow: str | None = Query(default=None),
    search: str | None = Query(default=None),
    date_from: datetime | None = Query(default=None),
    date_to: datetime | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    try:
        return PayflowCommunicationService(db).list_communications(
            user,
            client_id=client_id,
            account_id=account_id,
            status=status_filter,
            channel=channel,
            purpose=purpose,
            workflow=workflow,
            search=search,
            date_from=date_from,
            date_to=date_to,
            limit=limit,
            offset=offset,
        )
    except AppError as exc:
        raise _map_error(exc) from exc


@router.get("/comms/{communication_id}", response_model=PayflowCommunicationItem)
def get_comm(communication_id: int, user: RequirePayflowProduct, db: DbSession):
    try:
        return PayflowCommunicationService(db).get_communication(user, communication_id)
    except AppError as exc:
        raise _map_error(exc) from exc


@router.get("/accounts/{account_id}/communication-eligibility", response_model=CommunicationEligibilitySnapshot)
def get_account_communication_eligibility(
    account_id: int, user: RequirePayflowProduct, db: DbSession
):
    try:
        from app.infrastructure.database.models import PayflowAccountModel
        from sqlalchemy.orm import joinedload

        row = (
            db.query(PayflowAccountModel)
            .options(joinedload(PayflowAccountModel.client))
            .filter(PayflowAccountModel.id == account_id)
            .first()
        )
        if not row:
            raise NotFoundError("Account not found")
        return PayflowCommunicationService(db).case_eligibility_snapshot(user, row)
    except AppError as exc:
        raise _map_error(exc) from exc


@router.post(
    "/accounts/{account_id}/communications/sms/send",
    response_model=ManualSmsSendResponse,
)
def send_manual_sms(
    account_id: int,
    payload: ManualSmsSendRequest,
    user: RequirePayflowProduct,
    db: DbSession,
):
    try:
        from app.infrastructure.database.models import PayflowAccountModel
        from sqlalchemy.orm import joinedload

        row = (
            db.query(PayflowAccountModel)
            .options(joinedload(PayflowAccountModel.client))
            .filter(PayflowAccountModel.id == account_id)
            .first()
        )
        if not row:
            raise NotFoundError("Account not found")
        result = PayflowCommunicationService(db).send_manual_sms(
            user,
            row,
            body=payload.body,
            purpose=payload.purpose,
            include_payment_link=payload.include_payment_link,
            idempotency_key=payload.idempotency_key,
        )
        return ManualSmsSendResponse(
            skipped=result.get("skipped"),
            communication_id=result.get("communication_id"),
            code=result.get("code"),
            status=result.get("status"),
            comm_status=result.get("comm_status"),
            recipient=result.get("recipient"),
            provider_error=result.get("provider_error"),
            restriction_reason=result.get("restriction_reason"),
        )
    except AppError as exc:
        raise _map_error(exc) from exc


# ---------------------------------------------------------------------------
# Operational exceptions
# ---------------------------------------------------------------------------


@router.get("/exceptions", response_model=PayflowOperationalExceptionsListResponse)
def list_operational_exceptions(
    user: RequirePayflowProduct,
    db: DbSession,
    status_filter: str | None = Query(default=None, alias="status"),
    exception_type: str | None = Query(default=None),
    client_id: int | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    try:
        return PayflowOperationalExceptionService(db).list_exceptions(
            user,
            status=status_filter,
            exception_type=exception_type,
            client_id=client_id,
            limit=limit,
            offset=offset,
        )
    except AppError as exc:
        raise _map_error(exc) from exc


@router.get("/exceptions/{exception_id}", response_model=PayflowOperationalExceptionItem)
def get_operational_exception(
    exception_id: int, user: RequirePayflowProduct, db: DbSession
):
    try:
        return PayflowOperationalExceptionService(db).get_exception(user, exception_id)
    except AppError as exc:
        raise _map_error(exc) from exc


@router.post("/exceptions/{exception_id}/acknowledge", response_model=PayflowOperationalExceptionItem)
def acknowledge_operational_exception(
    exception_id: int, user: RequirePayflowProduct, db: DbSession
):
    try:
        return PayflowOperationalExceptionService(db).acknowledge(user, exception_id)
    except AppError as exc:
        raise _map_error(exc) from exc


@router.post("/exceptions/{exception_id}/resolve", response_model=PayflowOperationalExceptionItem)
def resolve_operational_exception(
    exception_id: int,
    user: RequirePayflowProduct,
    db: DbSession,
    payload: ResolveOperationalExceptionRequest = ResolveOperationalExceptionRequest(),
):
    try:
        return PayflowOperationalExceptionService(db).resolve(
            user, exception_id, note=payload.note
        )
    except AppError as exc:
        raise _map_error(exc) from exc


# ---------------------------------------------------------------------------
# Workflow worker / runs
# ---------------------------------------------------------------------------


@router.get("/workflow-worker-logs", response_model=PayflowWorkflowWorkerLogsResponse)
def list_workflow_worker_logs(
    user: RequirePayflowProduct,
    db: DbSession,
    client_id: int | None = Query(default=None),
    portfolio_id: int | None = Query(default=None),
    account_id: int | None = Query(default=None),
    strategy_id: int | None = Query(default=None),
    event_type: str | None = Query(default=None),
    level: str | None = Query(default=None),
    search: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=500),
):
    from app.modules.payflow.services.workflow_execution_service import WorkflowExecutionService

    items = WorkflowExecutionService(db).list_worker_logs(
        client_id=client_id,
        portfolio_id=portfolio_id,
        account_id=account_id,
        strategy_id=strategy_id,
        event_type=event_type,
        level=level,
        search=search,
        limit=limit,
    )
    return {"items": items}


@router.get("/workflow-runs", response_model=PayflowWorkflowRunsResponse)
def list_workflow_runs(
    user: RequirePayflowProduct,
    db: DbSession,
    client_id: int | None = Query(default=None),
    strategy_id: int | None = Query(default=None),
    account_id: int | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=100, ge=1, le=500),
):
    from app.modules.payflow.services.workflow_execution_service import WorkflowExecutionService

    items = WorkflowExecutionService(db).list_runs(
        client_id=client_id,
        strategy_id=strategy_id,
        account_id=account_id,
        status=status_filter,
        limit=limit,
    )
    return {"items": items}


@router.get("/workflow-runs/{run_id}", response_model=PayflowWorkflowRunItem)
def get_workflow_run(run_id: int, user: RequirePayflowProduct, db: DbSession):
    from app.core.exceptions import NotFoundError
    from app.modules.payflow.services.workflow_execution_service import WorkflowExecutionService

    item = WorkflowExecutionService(db).get_run(run_id)
    if not item:
        raise _map_error(NotFoundError("Workflow run not found"))
    return item


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------


@router.get("/notifications", response_model=PayflowNotificationsListResponse)
def list_notifications(user: RequirePayflowProduct, db: DbSession):
    try:
        return PayflowNotificationService(db).list_for_user(user)
    except AppError as exc:
        raise _map_error(exc) from exc


@router.post("/notifications/{notification_id}/read", response_model=PayflowNotificationItem)
def mark_notification_read(
    notification_id: int, user: RequirePayflowProduct, db: DbSession
):
    try:
        return PayflowNotificationService(db).mark_read(user, notification_id)
    except AppError as exc:
        raise _map_error(exc) from exc


@router.post("/notifications/read-all", response_model=PayflowNotificationsListResponse)
def mark_all_notifications_read(user: RequirePayflowProduct, db: DbSession):
    try:
        return PayflowNotificationService(db).mark_all_read(user)
    except AppError as exc:
        raise _map_error(exc) from exc


@router.get("/users", response_model=PayflowUsersListResponse)
def list_users(
    _: PayflowManageUsers,
    db: DbSession,
    search: str | None = Query(default=None),
    role_code: str | None = Query(default=None),
    status: str | None = Query(default=None),
    client_id: int | None = Query(default=None),
):
    return PayflowUserService(db).list_users(
        search=search, role_code=role_code, status=status, client_id=client_id
    )


@router.post("/users", response_model=PayflowUserDetailResponse)
def create_user(payload: CreatePayflowUserRequest, admin: PayflowOpsAdmin, db: DbSession):
    """Only Operations Admin may add users (not Supervisors with manage_users)."""
    try:
        return PayflowUserService(db).create_user(
            admin,
            full_name=payload.full_name,
            email=str(payload.email),
            role_code=payload.role_code,
            status=payload.status,
            client_ids=payload.client_ids,
            permission_codes=payload.permission_codes,
        )
    except AppError as exc:
        raise _map_error(exc) from exc


@router.get("/users/{user_id}", response_model=PayflowUserDetailResponse)
def get_user(user_id: UUID, _: PayflowManageUsers, db: DbSession):
    try:
        return PayflowUserService(db).get_user(user_id)
    except AppError as exc:
        raise _map_error(exc) from exc


@router.post("/users/{user_id}/update", response_model=PayflowUserDetailResponse)
def update_user(
    user_id: UUID,
    payload: UpdatePayflowUserRequest,
    actor: PayflowManageUsers,
    db: DbSession,
):
    try:
        return PayflowUserService(db).update_user(
            user_id,
            actor=actor,
            full_name=payload.full_name,
            # Email is immutable — not applied even if sent.
            email=None,
            role_code=payload.role_code,
            confirm_role_change=payload.confirm_role_change,
        )
    except AppError as exc:
        raise _map_error(exc) from exc


@router.post("/users/{user_id}/resend-invitation", response_model=MessageResponse)
def resend_invitation(user_id: UUID, _: PayflowManageUsers, db: DbSession):
    try:
        return PayflowUserService(db).resend_invitation(user_id)
    except AppError as exc:
        raise _map_error(exc) from exc


@router.post("/users/{user_id}/deactivate", response_model=PayflowUserDetailResponse)
def deactivate_user(user_id: UUID, admin: PayflowManageUsers, db: DbSession):
    try:
        return PayflowUserService(db).deactivate_user(user_id, actor=admin)
    except AppError as exc:
        raise _map_error(exc) from exc


@router.post("/users/{user_id}/reactivate", response_model=PayflowUserDetailResponse)
def reactivate_user(user_id: UUID, admin: PayflowManageUsers, db: DbSession):
    try:
        return PayflowUserService(db).reactivate_user(user_id, actor=admin)
    except AppError as exc:
        raise _map_error(exc) from exc


@router.post("/users/{user_id}/delete", response_model=MessageResponse)
def delete_user(
    user_id: UUID,
    admin: PayflowManageUsers,
    db: DbSession,
    source: str | None = Query(default=None),
):
    try:
        return PayflowUserService(db).delete_user(user_id, actor=admin, source=source)
    except AppError as exc:
        raise _map_error(exc) from exc


@router.get("/deletion-logs", response_model=list[DeletionLogResponse])
def list_payflow_deletion_logs(
    _: PayflowOpsAdmin,
    db: DbSession,
    search: str | None = Query(default=None),
    entity_type: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=500),
):
    return PayflowUserService(db).list_deletion_logs(search, entity_type, limit)
