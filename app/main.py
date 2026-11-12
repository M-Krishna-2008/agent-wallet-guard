"""
Main FastAPI application entrypoint for Agent Wallet Guard.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlmodel import Session
from app.config import settings
from app.db import engine, init_db
from app.routes.audit import router as audit_router
from app.routes.demo import router as demo_router
from app.routes.mcp import router as mcp_router
from app.routes.payments import router as payments_router
from app.routes.rules import router as rules_router
from app.rules import ensure_ruleset_seeded

logger = logging.getLogger("uvicorn.error")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application lifespan context manager:
    1. Validates environment safety (refuses non-sandbox PayPal unless ALLOW_LIVE=true).
    2. Logs active runtime environment parameters (never secrets).
    3. Initializes database tables (PostgreSQL/SQLite) and seeds default policy ruleset.
    """
    paypal_env = settings.PAYPAL_ENV.strip().lower()
    if paypal_env != "sandbox" and not settings.ALLOW_LIVE:
        raise RuntimeError(
            f"Startup blocked: PAYPAL_ENV is set to '{settings.PAYPAL_ENV}', which is not 'sandbox'. "
            "To run against a non-sandbox environment, you must explicitly configure ALLOW_LIVE=true."
        )

    # Safe startup log line displaying environment mode without exposing secrets
    db_scheme = (
        settings.normalized_database_url.split("://")[0]
        if "://" in settings.normalized_database_url
        else "database"
    )
    logger.info(
        "Agent Wallet Guard starting | Mode: %s | PayPal Env: %s | Mock: %s | Demo Mode: %s | Agent Mode: %s | DB Scheme: %s",
        "LIVE" if paypal_env == "live" else "SANDBOX",
        settings.PAYPAL_ENV,
        settings.PAYPAL_MOCK,
        settings.DEMO_MODE,
        settings.AGENT_MODE,
        db_scheme,
    )

    init_db()
    with Session(engine) as session:
        ensure_ruleset_seeded(session)
    yield


app = FastAPI(
    title="Agent Wallet Guard",
    description="A spending firewall and security guardrail between AI agents and PayPal.",
    version="0.1.0",
    lifespan=lifespan,
)

# Enable CORS for all origins (demo mode)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health", tags=["System"])
def health_check():
    """Health status endpoint."""
    return {"ok": True}


@app.get("/api/v1/info", tags=["System"])
def system_info():
    """Public system info and runtime configuration for dashboard."""
    from app.config import settings
    return {
        "ok": True,
        "demo_mode": settings.DEMO_MODE,
        "paypal_env": settings.PAYPAL_ENV,
        "paypal_mock": settings.PAYPAL_MOCK,
        "paypal_mode": settings.PAYPAL_MODE,
        "llm_provider": settings.LLM_PROVIDER,
        "llm_model": settings.LLM_MODEL,
        "agent_api_key": settings.AGENT_API_KEY if settings.DEMO_MODE else "",
        "vendor_email": settings.VENDOR_SANDBOX_EMAIL,
    }


# Mount API routes under /api/v1
app.include_router(payments_router, prefix="/api/v1")
app.include_router(rules_router, prefix="/api/v1")
app.include_router(audit_router, prefix="/api/v1")
app.include_router(mcp_router, prefix="/api/v1")
app.include_router(demo_router)

# UI Routes
static_dir = Path(__file__).resolve().parent.parent / "static"


@app.get("/app", tags=["UI"])
def dashboard_app():
    """Serve the interactive dashboard application."""
    app_html = static_dir / "app.html"
    if app_html.is_file():
        return FileResponse(str(app_html))
    return FileResponse(str(static_dir / "index.html"))


# Serve ./static at "/"
if static_dir.is_dir():
    app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")

