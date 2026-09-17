from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import settings
from app.infrastructure.database.seeder import run_seeder
from app.modules.identity.routes import router as identity_router
from app.modules.payflow.routes import router as payflow_router
from app.modules.platform.routes import router as platform_router


@asynccontextmanager
async def lifespan(_app: FastAPI):
    run_seeder()
    yield


app = FastAPI(
    title=settings.APP_NAME,
    description=(
        "Platform Suite API — identity, product entitlement, and product entry gates.\n\n"
        "## Auth (Swagger)\n"
        "1. Call `POST /auth/login`\n"
        "2. Copy **access_token** only (not refresh_token, not the whole JSON)\n"
        "3. Click **Authorize** → paste token → Authorize\n"
        "4. Do **not** type the word `Bearer` — Swagger adds it automatically"
    ),
    version="0.1.0",
    lifespan=lifespan,
    swagger_ui_parameters={"persistAuthorization": True},
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(identity_router)
app.include_router(platform_router)
app.include_router(payflow_router)


@app.get("/health", tags=["Health"])
def health_check():
    return {"status": "ok", "app": settings.APP_NAME}
