"""Email subject performance API: Dashboard section, subject details, AI insights, subject
suggestions for the workflow editor, and AI proposals for weak email steps.

Follows the PayFlow convention: GET reads, POST acts. Every endpoint only returns data of the
clients the user is allowed to see. Rates are fractions (0.425 = 42.5%).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from app.core.deps import DbSession
from app.core.exceptions import AppError, ForbiddenError
from app.modules.payflow.deps import PayflowManageWorkflows, RequirePayflowProduct
from app.modules.payflow.services.email_subject_ai import (
    PayflowEmailSubjectAiService,
    scan_in_background_if_due,
)
from app.modules.payflow.services.email_subject_service import PayflowEmailSubjectService, SubjectFilters

router = APIRouter(prefix="/payflow", tags=["PayFlow · Email Subject Performance"])

_STATUS = {
    "unauthorized": status.HTTP_401_UNAUTHORIZED,
    "forbidden": status.HTTP_403_FORBIDDEN,
    "not_found": status.HTTP_404_NOT_FOUND,
    "conflict": status.HTTP_409_CONFLICT,
    "validation_error": status.HTTP_400_BAD_REQUEST,
    "service_unavailable": status.HTTP_502_BAD_GATEWAY,
}


def _http(exc: AppError) -> HTTPException:
    return HTTPException(status_code=_STATUS.get(exc.code, 400), detail=exc.message)


# --------------------------------------------------------------------------- schemas


class SubjectMetrics(BaseModel):
    """Counts and rates of a group of emails. Rates are fractions of Delivered."""

    sent: int = 0
    delivered: int = 0
    opens: int = 0  # unique: emails opened at least once (estimated)
    open_rate: float = 0.0
    clicks: int = 0  # unique: emails with at least one click
    click_rate: float = 0.0
    payment_link_clicks: int = 0
    initiated: int = 0
    completed: int = 0  # successful payment transactions credited to these emails
    emails_with_payment: int = 0  # emails that led to at least one of them
    conversion: float = 0.0
    manual_sent: int = 0
    low_sample: bool = False


class SubjectRow(SubjectMetrics):
    subject_key: str
    subject: str
    variations: int = 1
    contexts: List[str] = Field(default_factory=list)
    reminder_stages: List[str] = Field(default_factory=list)


class SubjectLeaders(BaseModel):
    opened: Optional[SubjectRow] = None
    clicked: Optional[SubjectRow] = None
    payments: Optional[SubjectRow] = None
    conversion: Optional[SubjectRow] = None


class Measurement(BaseModel):
    min_deliveries: int
    attribution_window_days: int
    attribution_rule: str
    rate_denominator: str
    opens_are_estimated: bool


class NamedOption(BaseModel):
    id: int
    name: str
    client_id: Optional[int] = None


class FilterOptions(BaseModel):
    date_ranges: List[str]
    clients: List[NamedOption]
    portfolios: List[NamedOption]
    workflows: List[NamedOption]
    reminder_stages: List[str]
    purposes: List[str]


class EmailSubjectsResponse(BaseModel):
    filter_options: FilterOptions
    measurement: Measurement
    leaders: SubjectLeaders
    rows: List[SubjectRow]
    total: int
    unattributed_payments: int


class FunnelStep(BaseModel):
    label: str
    value: int
    pct_of_sent: float


class TrendPoint(BaseModel):
    date: str
    opens: int
    clicks: int
    payments: int


class BreakdownLine(BaseModel):
    id: Optional[Any] = None
    label: str
    delivered: int
    completed: int
    conversion: float


class Breakdown(BaseModel):
    by_client: List[BreakdownLine]
    by_workflow: List[BreakdownLine]
    by_stage: List[BreakdownLine]


class BodyVariation(SubjectMetrics):
    id: Optional[int] = None
    label: str
    subject: Optional[str] = None
    body: Optional[str] = None
    origin: Optional[str] = None
    language: Optional[str] = None
    reminder_stage: Optional[str] = None
    first_used_at: Optional[datetime] = None
    last_used_at: Optional[datetime] = None


class EmailSubjectDetailsResponse(BaseModel):
    subject_key: str
    subject: str
    reminder_stages: List[str]
    contexts: List[str]
    summary: SubjectRow
    funnel: List[FunnelStep]
    period: str
    trend: List[TrendPoint]
    breakdown: Breakdown
    variations: List[BodyVariation]
    similar: List[SubjectRow]
    measurement: Measurement


class InsightRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    scope: str = "subject"  # subject | kpi | section
    subject_key: Optional[str] = None
    metric: Optional[str] = None  # kpi: opens | clicks | completed | conversion
    date_range: str = "30d"
    date_from: Optional[date] = None
    date_to: Optional[date] = None
    client_id: Optional[int] = None
    portfolio_ids: List[int] = Field(default_factory=list)
    strategy_id: Optional[int] = None
    reminder_stage: Optional[str] = None
    purpose: Optional[str] = None
    search: Optional[str] = None


class InsightResponse(BaseModel):
    subject_key: Optional[str] = None
    subject: Optional[str] = None
    insight: str
    observations: List[str] = Field(default_factory=list)
    insufficient_data: bool = False
    evidence: dict = Field(default_factory=dict)
    generated_by: str  # "ai" | "rules" (fixed wording when the AI is unavailable or data is too thin)
    cached: bool = False  # the same numbers were explained a moment ago: that answer is reused
    disclaimer: str


class SubjectSuggestion(BaseModel):
    subject: str
    explanation: str
    measured: bool  # False: a new AI variation of source_subject, not yet sent
    source_subject: str
    metrics: SubjectMetrics
    low_sample: bool = False
    body: Optional[str] = None  # the body that did best under a measured subject
    body_origin: Optional[str] = None


class SubjectSuggestionsResponse(BaseModel):
    client_id: int
    strategy_id: Optional[int] = None
    reminder_stage: str
    purpose: str
    scope: str
    rows: List[SubjectSuggestion]
    ai_generated: bool
    disclaimer: str


class MessageText(BaseModel):
    subject: Optional[str] = None
    body: Optional[str] = None
    origin: Optional[str] = None


class ProposalItem(BaseModel):
    id: int
    client_id: int
    strategy_id: int
    strategy_name: Optional[str] = None
    step_id: str
    step_title: Optional[str] = None
    recommendation: str  # Retain | Revise | Test alternative subjects | Replace | Insufficient data
    status: str  # Pending | Accepted | Edited | Rejected | Superseded
    existing: MessageText
    proposed: Optional[MessageText] = None
    alternative_subjects: List[dict] = Field(default_factory=list)
    explanation: Optional[str] = None
    evidence: dict = Field(default_factory=dict)
    final: Optional[MessageText] = None
    decision_note: Optional[str] = None
    created_by: Optional[str] = None
    created_at: Optional[datetime] = None
    decided_by: Optional[str] = None
    decided_at: Optional[datetime] = None


class WorkflowOptimizationResponse(BaseModel):
    strategy_id: int
    strategy_name: str
    proposals: List[ProposalItem]
    disclaimer: str


class ProposalsListResponse(BaseModel):
    items: List[ProposalItem]
    total: int
    pending: int = 0


class TrendSeriesPoint(BaseModel):
    date: str
    delivered: int
    opens: int
    clicks: int
    payments: int
    conversion: float


class TrendSeries(BaseModel):
    id: Optional[Any] = None
    label: str
    totals: dict
    points: List[TrendSeriesPoint]


class EmailSubjectsTrendResponse(BaseModel):
    group_by: str
    period: str
    dates: List[str]
    series: List[TrendSeries]
    more_groups: int = 0


class NewStepDraftRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    client_id: int
    purpose: Optional[str] = None
    reminder_stage: Optional[str] = None
    remarks: Optional[str] = None


class ProposalDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    action: str  # accept | edit | reject
    subject: Optional[str] = Field(default=None, max_length=500)  # edit: the user's version
    body: Optional[str] = None
    note: Optional[str] = None


class EmailDraftRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    step_id: str
    remarks: Optional[str] = None


class EmailDraftResponse(BaseModel):
    strategy_id: Optional[int] = None  # None: a draft for a workflow that is not saved yet
    step_id: str
    existing: MessageText
    subject: str
    body: Optional[str] = None
    origin: Optional[str] = None
    generated_by: str  # "ai" | "history" (the client's best measured email, when the AI is unavailable)
    explanation: str
    evidence: dict = Field(default_factory=dict)
    saved: bool = False  # always False: a draft is never put on the workflow by this call
    disclaimer: str


class LowPerformerItem(BaseModel):
    client_id: int
    client_name: Optional[str] = None
    strategy_id: int
    strategy_name: Optional[str] = None
    step_id: str
    step_title: Optional[str] = None
    subject: str
    reminder_stage: str
    purpose: str
    recommendation: str
    reason: str
    metrics: Optional[SubjectMetrics] = None
    comparable_templates: Optional[dict] = None
    pending_proposal_id: Optional[int] = None


class LowPerformersResponse(BaseModel):
    items: List[LowPerformerItem]
    total: int
    min_deliveries: int


class ScanResponse(BaseModel):
    workflows: int
    proposals: int


class MessageHistoryItem(SubjectMetrics):
    id: int
    step_id: Optional[str] = None
    strategy_version: Optional[int] = None
    reminder_stage: Optional[str] = None
    purpose: Optional[str] = None
    language: Optional[str] = None
    origin: Optional[str] = None
    subject: Optional[str] = None
    body: Optional[str] = None
    first_used_at: Optional[datetime] = None
    last_used_at: Optional[datetime] = None
    is_current_subject: bool = False


class MessageHistoryResponse(BaseModel):
    strategy_id: int
    strategy_name: str
    items: List[MessageHistoryItem]
    total: int


# --------------------------------------------------------------------------- filters


def _filters(
    user: RequirePayflowProduct,
    db: DbSession,
    date_range: str = Query(default="30d", description="30d | 7d | today | custom"),
    date_from: Optional[date] = Query(default=None, description="custom range: first day"),
    date_to: Optional[date] = Query(default=None, description="custom range: last day"),
    client_id: Optional[int] = Query(default=None),
    portfolio_ids: List[int] = Query(default_factory=list, description="applies with client_id"),
    strategy_id: Optional[int] = Query(default=None),
    reminder_stage: Optional[str] = Query(default=None),
    purpose: Optional[str] = Query(default=None, description="email purpose, e.g. Payment Reminder"),
    search: Optional[str] = Query(default=None),
) -> SubjectFilters:
    try:
        return PayflowEmailSubjectService(db).build_filters(
            user,
            date_range=date_range,
            date_from=date_from,
            date_to=date_to,
            client_id=client_id,
            portfolio_ids=portfolio_ids,
            strategy_id=strategy_id,
            reminder_stage=reminder_stage,
            purpose=purpose,
            search=search,
        )
    except AppError as exc:
        raise _http(exc) from exc


Filters = Annotated[SubjectFilters, Depends(_filters)]


# --------------------------------------------------------------------------- dashboard


@router.get("/dashboard/email-subjects", response_model=EmailSubjectsResponse)
def get_email_subjects(
    user: RequirePayflowProduct,
    db: DbSession,
    filters: Filters,
    sort: str = Query(default="completed"),
    order: str = Query(default="desc"),
):
    """The Dashboard's Email Subject Performance section: KPI cards, table and filter options."""
    try:
        return PayflowEmailSubjectService(db).get_section(user, filters, sort=sort, order=order)
    except AppError as exc:
        raise _http(exc) from exc


@router.get("/dashboard/email-subjects/details", response_model=EmailSubjectDetailsResponse)
def get_email_subject_details(
    user: RequirePayflowProduct,
    db: DbSession,
    filters: Filters,
    subject_key: str = Query(..., min_length=1),
    period: str = Query(default="daily", description="daily | weekly"),
):
    """One subject's popup: summary, funnel, trend, breakdowns, body variations, similar subjects."""
    try:
        return PayflowEmailSubjectService(db).get_details(user, filters, subject_key, period=period)
    except AppError as exc:
        raise _http(exc) from exc


@router.get("/dashboard/email-subjects/trend", response_model=EmailSubjectsTrendResponse)
def get_email_subjects_trend(
    user: RequirePayflowProduct,
    db: DbSession,
    filters: Filters,
    group_by: str = Query(default="overall", description="overall | client | workflow | stage"),
    period: str = Query(default="daily", description="daily | weekly"),
):
    """How the selected emails did over time: as a whole, or one line per client / workflow /
    reminder stage."""
    try:
        return PayflowEmailSubjectService(db).get_trend(user, filters, group_by=group_by, period=period)
    except AppError as exc:
        raise _http(exc) from exc


@router.post("/dashboard/email-subjects/insight", response_model=InsightResponse)
def generate_email_subject_insight(payload: InsightRequest, user: RequirePayflowProduct, db: DbSession):
    """Generate AI Insights for a table row / the popup ("subject"), a KPI card ("kpi") or the
    whole section ("section"), from the tracked numbers of the selected filters."""
    try:
        filters = PayflowEmailSubjectService(db).build_filters(
            user,
            date_range=payload.date_range,
            date_from=payload.date_from,
            date_to=payload.date_to,
            client_id=payload.client_id,
            portfolio_ids=payload.portfolio_ids,
            strategy_id=payload.strategy_id,
            reminder_stage=payload.reminder_stage,
            purpose=payload.purpose,
            search=payload.search,
        )
        return PayflowEmailSubjectAiService(db).insight(
            user, filters, scope=payload.scope, subject=payload.subject_key, metric=payload.metric
        )
    except AppError as exc:
        raise _http(exc) from exc


# --------------------------------------------------------------------------- workflow editor


@router.get("/email-subjects/suggestions", response_model=SubjectSuggestionsResponse)
def get_subject_suggestions(
    user: RequirePayflowProduct,
    db: DbSession,
    client_id: int = Query(...),
    strategy_id: Optional[int] = Query(default=None, description="omit for a workflow not saved yet"),
    reminder_stage: Optional[str] = Query(default=None),
    purpose: Optional[str] = Query(default=None),
):
    """Suggested subjects for a workflow email step, from this client's own history."""
    try:
        return PayflowEmailSubjectAiService(db).suggestions(
            user, client_id=client_id, strategy_id=strategy_id, reminder_stage=reminder_stage, purpose=purpose
        )
    except AppError as exc:
        raise _http(exc) from exc


@router.get("/workflows/{strategy_id}/message-history", response_model=MessageHistoryResponse)
def get_workflow_message_history(
    strategy_id: int,
    user: RequirePayflowProduct,
    db: DbSession,
    step_id: Optional[str] = Query(default=None),
):
    """Every distinct email text the workflow has sent (old versions keep their results)."""
    try:
        return PayflowEmailSubjectAiService(db).message_history(user, strategy_id, step_id)
    except AppError as exc:
        raise _http(exc) from exc


@router.post("/email-subjects/draft", response_model=EmailDraftResponse)
def draft_email_for_new_step(payload: NewStepDraftRequest, user: PayflowManageWorkflows, db: DbSession):
    """A subject and body for an email step of a workflow that is not saved yet. Nothing is saved."""
    try:
        return PayflowEmailSubjectAiService(db).draft_for_new_step(
            user,
            client_id=payload.client_id,
            purpose=payload.purpose,
            reminder_stage=payload.reminder_stage,
            remarks=payload.remarks,
        )
    except AppError as exc:
        raise _http(exc) from exc


@router.post("/workflows/{strategy_id}/email-suggestion", response_model=EmailDraftResponse)
def draft_workflow_email(
    strategy_id: int, payload: EmailDraftRequest, user: PayflowManageWorkflows, db: DbSession
):
    """A new subject and body for one email step, from this client's measured results.
    Nothing is saved: the user decides whether to use it."""
    try:
        return PayflowEmailSubjectAiService(db).draft_email(user, strategy_id, payload.step_id, payload.remarks)
    except AppError as exc:
        raise _http(exc) from exc


# --------------------------------------------------------------------------- AI proposals


@router.get("/email-subjects/low-performers", response_model=LowPerformersResponse)
def list_low_performing_emails(
    user: RequirePayflowProduct,
    db: DbSession,
    client_id: Optional[int] = Query(default=None),
    include_all: bool = Query(default=False, description="also the steps that are doing fine"),
):
    """Email steps of Active workflows that under-perform comparable templates (same client,
    reminder stage and purpose). Read-only."""
    try:
        scan_in_background_if_due()  # the automatic check also runs where the worker does not
        return PayflowEmailSubjectAiService(db).low_performers(user, client_id=client_id, include_all=include_all)
    except AppError as exc:
        raise _http(exc) from exc


@router.post("/email-subjects/low-performers/scan", response_model=ScanResponse)
def scan_low_performing_emails(user: RequirePayflowProduct, db: DbSession):
    """Run now the check the worker runs on its own (Operations Admin): every Active workflow's
    under-performing email steps get a proposal for review. Workflows are not changed."""
    try:
        service = PayflowEmailSubjectAiService(db)
        if not service.access.is_operations_admin(user):
            raise ForbiddenError("Operations Admin only")
        return service.scan_low_performers()
    except AppError as exc:
        raise _http(exc) from exc


@router.post("/workflows/{strategy_id}/email-optimization", response_model=WorkflowOptimizationResponse)
def analyze_workflow_emails(strategy_id: int, user: PayflowManageWorkflows, db: DbSession):
    """Compare the workflow's email steps with comparable templates and store a proposal per step
    (Retain / Revise / Test alternative subjects / Replace). The workflow is not changed."""
    try:
        return PayflowEmailSubjectAiService(db).analyze_workflow(user, strategy_id)
    except AppError as exc:
        raise _http(exc) from exc


@router.get("/email-proposals", response_model=ProposalsListResponse)
def list_email_proposals(
    user: RequirePayflowProduct,
    db: DbSession,
    strategy_id: Optional[int] = Query(default=None),
    client_id: Optional[int] = Query(default=None),
    status_filter: Optional[str] = Query(default=None, alias="status"),
):
    try:
        scan_in_background_if_due()
        return PayflowEmailSubjectAiService(db).list_proposals(
            user, strategy_id=strategy_id, client_id=client_id, status=status_filter
        )
    except AppError as exc:
        raise _http(exc) from exc


@router.post("/email-proposals/{proposal_id}/decide", response_model=ProposalItem)
def decide_email_proposal(
    proposal_id: int, payload: ProposalDecisionRequest, user: PayflowManageWorkflows, db: DbSession
):
    """Accept, edit or reject a proposal. Accepted text goes through the normal workflow update:
    an Active or Approved workflow is archived as a version and returns to review."""
    try:
        return PayflowEmailSubjectAiService(db).decide(
            user,
            proposal_id,
            action=payload.action,
            subject=payload.subject,
            body=payload.body,
            note=payload.note,
        )
    except AppError as exc:
        raise _http(exc) from exc
