"""FastAPI application factory."""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from tablement.web import db
from tablement.web.state import JobManager

logger = logging.getLogger(__name__)
WEB_DIR = Path(__file__).parent


def _load_dotenv():
    """Load .env file from project root if it exists."""
    # Walk up from this file to find .env
    env_path = WEB_DIR.parent.parent.parent / ".env"
    if not env_path.exists():
        # Try CWD
        env_path = Path.cwd() / ".env"
    if env_path.exists():
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    key, value = key.strip(), value.strip()
                    if key not in os.environ:  # Don't override existing env vars
                        os.environ[key] = value
        logger.info("Loaded .env from %s", env_path)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Load .env file
    _load_dotenv()

    # Initialize Supabase client
    try:
        await db.init_supabase()
    except RuntimeError as e:
        logger.warning("Supabase not configured: %s", e)
        logger.warning("Auth and DB features will not work until env vars are set")

    # Initialize job manager (in-memory tracking of active snipe/monitor jobs)
    app.state.jobs = JobManager()

    # Initialize APScheduler for timed jobs
    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler

        scheduler = AsyncIOScheduler(timezone="UTC")
        scheduler.start()
        app.state.scheduler = scheduler
        logger.info("APScheduler started")
    except ImportError:
        logger.warning("APScheduler not installed — scheduled jobs disabled")
        app.state.scheduler = None

    # Start Drop Intelligence collector
    try:
        from tablement.web.drop_intel import drop_intel_collector
        await drop_intel_collector.start(app)
        app.state.drop_intel = drop_intel_collector
        logger.info("Drop Intelligence collector started")
    except Exception as e:
        logger.warning("Drop Intelligence collector failed to start: %s", e)
        app.state.drop_intel = None

    yield

    # Shutdown: cancel all running jobs
    app.state.jobs.cancel_all()
    if app.state.scheduler:
        app.state.scheduler.shutdown(wait=False)

    # Stop Drop Intelligence collector
    if getattr(app.state, "drop_intel", None):
        await app.state.drop_intel.stop()


def create_app() -> FastAPI:
    app = FastAPI(title="Tablement", lifespan=lifespan)

    from tablement.web.routes import admin, auth, policy, reservations, venue

    app.include_router(auth.router, prefix="/api/auth")
    app.include_router(venue.router, prefix="/api/venue")
    app.include_router(policy.router, prefix="/api/policy")
    app.include_router(reservations.router, prefix="/api/reservations")
    app.include_router(admin.router, prefix="/api/admin")

    # Payment routes (optional — only if stripe is configured)
    try:
        from tablement.web.routes import payments

        app.include_router(payments.router, prefix="/api/payments")
    except ImportError:
        logger.info("Stripe not configured — payment routes disabled")

    static_dir = WEB_DIR / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=static_dir), name="static")

    templates = Jinja2Templates(directory=WEB_DIR / "templates")

    @app.get("/")
    async def landing(request: Request):
        return templates.TemplateResponse("landing.html", {"request": request})

    @app.get("/app")
    async def dashboard(request: Request):
        return templates.TemplateResponse("dashboard.html", {"request": request})

    @app.get("/admin")
    async def admin_page(request: Request):
        return templates.TemplateResponse("admin.html", {"request": request})

    @app.get("/admin/vip")
    async def admin_vip_page(request: Request):
        return templates.TemplateResponse("admin_vip.html", {"request": request})

    return app
