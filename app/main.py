import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.core import rate_limit
from app.core.config import settings
from app.core.openapi_tags import OPENAPI_TAGS, apply_openapi_tags
from app.core.request_context import (
    frontend_base_from_headers,
    reset_frontend_base,
    set_frontend_base,
)
from app.infrastructure.database.seeder import run_seeder
from app.modules.identity.routes import router as identity_router
from app.modules.payflow.compliance_routes import router as compliance_router
from app.modules.payflow.email_subject_routes import router as email_subject_router
from app.modules.payflow.routes import router as payflow_router
from app.modules.payflow.sendgrid_webhook import router as sendgrid_webhook_router
from app.modules.payflow.stripe_webhook import router as stripe_webhook_router
from app.modules.payflow.twilio_webhook import router as twilio_webhook_router
from app.modules.platform.routes import router as platform_router

logger = logging.getLogger("uvicorn.error")

_IS_PROD = (settings.APP_ENV or "").strip().lower() == "production"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if _IS_PROD:
        secret = (settings.JWT_SECRET or "").strip()
        if not secret or secret == "change-me-platform-suite-secret-key" or len(secret) < 32:
            raise RuntimeError(
                "Refusing to start in production: set a strong JWT_SECRET "
                "(at least 32 characters, not the default placeholder)."
            )
    # Additive only: create missing tables/columns + insert missing seed rows.
    # Existing org grants, people assignments, and product rows are never overwritten.
    logger.info("Startup: schema sync + seed (additive)…")
    run_seeder()
    logger.info("Startup: ready")
    yield


app = FastAPI(
    title=settings.APP_NAME,
    description=(
        "Platform Suite API — identity, product entitlement, and product modules.\n\n"
        "Swagger sections: **Auth**, **Platform · …**, **PayFlow · …** "
        "(grouped by area; URL paths are unchanged).\n\n"
        "## Auth (Swagger)\n"
        "1. Call `POST /auth/login`\n"
        "2. Copy **access_token** only (not refresh_token, not the whole JSON)\n"
        "3. Click **Authorize** → paste token → Authorize\n"
        "4. Do **not** type the word `Bearer` — Swagger adds it automatically"
    ),
    version="0.1.0",
    lifespan=lifespan,
    # Production: hide /docs, /redoc, /openapi.json
    docs_url=None if _IS_PROD else "/docs",
    redoc_url=None if _IS_PROD else "/redoc",
    openapi_url=None if _IS_PROD else "/openapi.json",
    swagger_ui_parameters=None if _IS_PROD else {"persistAuthorization": True},
    openapi_tags=None if _IS_PROD else OPENAPI_TAGS,
)

_cors_origins = settings.cors_allow_origins()
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _client_address(request: Request) -> str:
    """Caller's address for throttling. Behind a trusted proxy, the address it reports."""
    if settings.TRUST_FORWARDED_FOR:
        forwarded = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
        if forwarded:
            return forwarded
    return request.client.host if request.client else "unknown"


@app.middleware("http")
async def throttle_guessable_endpoints(request: Request, call_next):
    """Sign-in, password reset and public token pages: a few tries per address."""
    bucket = rate_limit.bucket_for(request.method, request.url.path)
    if bucket:
        wait = rate_limit.check(bucket, _client_address(request))
        if wait is not None:
            return JSONResponse(
                status_code=429,
                content={"detail": "Too many attempts. Please wait a moment and try again."},
                headers={"Retry-After": str(wait)},
            )
    return await call_next(request)


@app.middleware("http")
async def capture_frontend_origin(request: Request, call_next):
    """Use browser Origin/Referer so invite links match localhost vs ngrok."""
    base = frontend_base_from_headers(
        request.headers.get("origin"),
        request.headers.get("referer"),
    )
    token = set_frontend_base(base)
    try:
        response = await call_next(request)
        # Reduce leakage of payment / auth tokens via Referer on API responses.
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        return response
    finally:
        reset_frontend_base(token)

uploads_dir = Path(__file__).resolve().parents[1] / "uploads"
uploads_dir.mkdir(parents=True, exist_ok=True)
(uploads_dir / "avatars").mkdir(parents=True, exist_ok=True)
(uploads_dir / "client-logos").mkdir(parents=True, exist_ok=True)
(uploads_dir / "imports").mkdir(parents=True, exist_ok=True)


class PublicUploads(StaticFiles):
    """Serve only the folders meant for browsers (avatars, client logos).

    Import files (customer names, emails, balances) live under the same
    ``uploads`` root and must never be downloadable by URL.
    """

    PUBLIC_FOLDERS = ("avatars/", "client-logos/")

    async def get_response(self, path: str, scope):
        normalized = path.replace("\\", "/").lstrip("/")
        if not normalized.startswith(self.PUBLIC_FOLDERS):
            raise HTTPException(status_code=404)
        return await super().get_response(path, scope)


app.mount("/uploads", PublicUploads(directory=str(uploads_dir)), name="uploads")

app.include_router(identity_router)
app.include_router(platform_router)
app.include_router(payflow_router)
app.include_router(compliance_router)
app.include_router(email_subject_router)
app.include_router(sendgrid_webhook_router)
app.include_router(twilio_webhook_router)
app.include_router(stripe_webhook_router)


@app.get("/health", tags=["Health"])
def health_check():
    return {"status": "ok", "app": settings.APP_NAME}


if not _IS_PROD:

    def custom_openapi():
        """Regroup operations into Auth / Platform / PayFlow sections without changing paths."""
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title,
            version=app.version,
            description=app.description,
            routes=app.routes,
            tags=OPENAPI_TAGS,
        )
        app.openapi_schema = apply_openapi_tags(schema)
        return app.openapi_schema

    app.openapi = custom_openapi
