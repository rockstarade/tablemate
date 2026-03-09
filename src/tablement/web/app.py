"""FastAPI application factory."""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from tablement.web import db
from tablement.web.ratelimit import limiter
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

    # Start Scout orchestrator (continuous restaurant monitoring)
    try:
        from tablement.web.scout import scout_orchestrator
        await scout_orchestrator.start(app)
        app.state.scout = scout_orchestrator
        logger.info("Scout orchestrator started")
    except Exception as e:
        logger.warning("Scout orchestrator failed to start: %s", e)
        app.state.scout = None

    # Recover in-flight jobs from before crash/restart
    try:
        from tablement.web.routes.reservations import recover_jobs
        recovered = await recover_jobs(app)
        if recovered:
            logger.info("Recovered %d in-flight jobs", recovered)
    except Exception as e:
        logger.warning("Job recovery failed: %s", e)

    yield

    # Shutdown: cancel all running jobs
    app.state.jobs.cancel_all()
    if app.state.scheduler:
        app.state.scheduler.shutdown(wait=False)

    # Stop Drop Intelligence collector
    if getattr(app.state, "drop_intel", None):
        await app.state.drop_intel.stop()

    # Stop Scout orchestrator
    if getattr(app.state, "scout", None):
        await app.state.scout.stop()


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add security headers to all responses."""

    async def dispatch(self, request: Request, call_next):
        response: Response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' 'unsafe-eval' https://cdn.jsdelivr.net https://cdn.tailwindcss.com https://unpkg.com; "
            "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://cdn.tailwindcss.com https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com https://cdn.jsdelivr.net; "
            "img-src 'self' data: https:; "
            "connect-src 'self' https://*.supabase.co wss://*.supabase.co; "
            "frame-ancestors 'none'"
        )
        return response


def create_app() -> FastAPI:
    app = FastAPI(title="Tablement", lifespan=lifespan)

    # Rate limiting
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    # CORS — restrict to same origin (no cross-site API access)
    allowed_origins = os.environ.get("CORS_ORIGINS", "").split(",")
    allowed_origins = [o.strip() for o in allowed_origins if o.strip()]
    if not allowed_origins:
        # Default: same-origin only (no wildcard)
        allowed_origins = []
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "X-Admin-Key"],
    )

    # Security headers on every response
    app.add_middleware(SecurityHeadersMiddleware)

    from tablement.web.routes import admin, auth, curated, ot_auth, policy, reservations, slots, venue

    app.include_router(auth.router, prefix="/api/auth")
    app.include_router(ot_auth.router, prefix="/api/auth")
    app.include_router(venue.router, prefix="/api/venue")
    app.include_router(policy.router, prefix="/api/policy")
    app.include_router(reservations.router, prefix="/api/reservations")
    app.include_router(admin.router, prefix="/api/admin")
    app.include_router(curated.router, prefix="/api/curated")
    app.include_router(slots.router, prefix="/api/slots")

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

    @app.get("/v2")
    async def landing_v2(request: Request):
        return templates.TemplateResponse("landing_v2.html", {"request": request})

    @app.get("/v3")
    async def landing_v3(request: Request):
        return templates.TemplateResponse("landing_v3.html", {"request": request})

    @app.get("/v4")
    async def landing_v4(request: Request):
        return templates.TemplateResponse("landing_v4.html", {"request": request})

    @app.get("/v5")
    async def landing_v5(request: Request):
        return templates.TemplateResponse("landing_v5.html", {"request": request})

    @app.get("/v6")
    async def landing_v6(request: Request):
        return templates.TemplateResponse("landing_v6.html", {"request": request})

    @app.get("/v7")
    async def landing_v7(request: Request):
        return templates.TemplateResponse("landing_v7.html", {"request": request})

    @app.get("/v8")
    async def landing_v8(request: Request):
        return templates.TemplateResponse("landing_v8.html", {"request": request})

    @app.get("/admin")
    async def admin_page(request: Request):
        return templates.TemplateResponse("admin_vip.html", {"request": request})

    @app.get("/admin/vip")
    async def admin_vip_redirect(request: Request):
        return RedirectResponse("/admin", status_code=302)

    # --- Consumer-facing pages ---

    @app.get("/browse")
    async def browse_page(request: Request):
        return templates.TemplateResponse("browse.html", {
            "request": request,
            "stripe_pk": os.environ.get("STRIPE_PUBLISHABLE_KEY", ""),
        })

    @app.get("/restaurant/{venue_id}")
    async def restaurant_page(request: Request, venue_id: int):
        return templates.TemplateResponse("restaurant.html", {
            "request": request,
            "venue_id": venue_id,
            "stripe_pk": os.environ.get("STRIPE_PUBLISHABLE_KEY", ""),
        })

    @app.get("/reservations")
    async def reservations_page(request: Request):
        from fastapi.responses import RedirectResponse
        return RedirectResponse("/browse?panel=reservations", status_code=302)

    @app.get("/settings")
    async def settings_page(request: Request):
        from fastapi.responses import RedirectResponse
        return RedirectResponse("/browse?modal=settings", status_code=302)

    # --- Legal pages ---

    # --- SEO ---

    @app.get("/robots.txt", response_class=PlainTextResponse)
    async def robots_txt():
        return (
            "User-agent: *\n"
            "Allow: /\n"
            "Disallow: /api/\n"
            "Disallow: /admin\n"
            "Disallow: /browse\n"
            "Disallow: /settings\n"
            "Disallow: /reservations\n"
            "\n"
            "Sitemap: https://tablepass.nyc/sitemap.xml\n"
        )

    @app.get("/sitemap.xml")
    async def sitemap_xml():
        from starlette.responses import Response as StarletteResponse
        xml = '<?xml version="1.0" encoding="UTF-8"?>\n'
        xml += '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        pages = [
            ("https://tablepass.nyc/", "daily", "1.0"),
            ("https://tablepass.nyc/terms", "monthly", "0.3"),
            ("https://tablepass.nyc/privacy", "monthly", "0.3"),
        ]
        for url, freq, priority in pages:
            xml += f"  <url><loc>{url}</loc><changefreq>{freq}</changefreq><priority>{priority}</priority></url>\n"
        xml += "</urlset>"
        return StarletteResponse(content=xml, media_type="application/xml")

    # --- Legal pages ---

    @app.get("/terms")
    async def terms_page(request: Request):
        return templates.TemplateResponse("terms.html", {"request": request})

    @app.get("/privacy")
    async def privacy_page(request: Request):
        return templates.TemplateResponse("privacy.html", {"request": request})

    return app
