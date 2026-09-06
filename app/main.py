"""
app/main.py

FastAPI application entry point.

Responsibilities:
  - Lifespan hook (startup / graceful shutdown)
  - Middleware registration (RequestId)
  - Exception handler registration
  - Router mounting
"""

from __future__ import annotations

import html
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Depends, Form, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import OAuth2PasswordRequestForm
from fastapi.staticfiles import StaticFiles

from prometheus_fastapi_instrumentator import Instrumentator

from app.api.router import api_router
from app.core.config import get_settings
from app.core.dependencies import set_access_cookie, safe_next_path
from app.core.login_template import LOGIN_HTML
from app.core.exceptions import (
    AuthException,
    BaseAppException,
    app_exception_handler,
    unhandled_exception_handler,
)
from app.core.logging import RequestIdMiddleware, setup_logging
from app.domain.models import OAuth2TokenResponse
from app.repositories.json_user_repository import JsonUserRepository
from app.services.auth_service import AuthService
from app.services.command_service import CommandService

_logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Lifespan
# ──────────────────────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Application lifespan manager.

    Startup:  configure logging, validate config.
    Shutdown: emit a graceful-shutdown log so orchestrators know we're done.
    """
    settings = get_settings()
    setup_logging(settings.LOG_LEVEL)

    _logger.info(
        "Starting %s v%s | env=%s",
        settings.APP_NAME,
        settings.APP_VERSION,
        settings.APP_ENV,
    )

    yield  # ← application runs here

    _logger.info("Shutting down %s — goodbye.", settings.APP_NAME)

    from app.core.redis_client import RedisClient
    from app.repositories.command_state_repository import CommandStateRepository

    try:
        redis = await RedisClient.get_client()
        repo = CommandStateRepository(redis)
        svc = CommandService(repo)
        await svc.shutdown_gracefully()
    # Shutdown must never raise: a failure here would abort the lifespan teardown and leave Redis unclosed.
    except Exception as e:  # pylint: disable=broad-exception-caught
        _logger.error(f"Error during graceful shutdown: {e}")

    await RedisClient.close()


# ──────────────────────────────────────────────────────────────────────────────
# App factory
# ──────────────────────────────────────────────────────────────────────────────


def create_app() -> FastAPI:
    settings = get_settings()

    # Shadows the module-level `app = create_app()` below; that is the
    # app-factory pattern, and renaming either would be worse.
    app = FastAPI(  # pylint: disable=redefined-outer-name
        title=settings.APP_NAME,
        version=settings.APP_VERSION,
        debug=settings.DEBUG,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )

    # ── Static files ───────────────────────────────────────────────────────────
    app.mount("/static", StaticFiles(directory="app/static"), name="static")

    # ── Middleware ─────────────────────────────────────────────────────────────
    app.add_middleware(RequestIdMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # Tighten per environment as needed
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── Exception handlers ─────────────────────────────────────────────────────
    app.add_exception_handler(BaseAppException, app_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, unhandled_exception_handler)

    # ── Health check ───────────────────────────────────────────────────────────
    @app.get("/health", tags=["system"], summary="Health check")
    async def health_check() -> dict[str, str]:
        return {"status": "ok"}

    # ── Offline Documentation ──────────────────────────────────────────────────
    if settings.DEBUG:

        @app.get("/docs", include_in_schema=False)
        async def custom_swagger_ui_html():
            return get_swagger_ui_html(
                openapi_url=app.openapi_url,  # type: ignore
                title=app.title + " - Swagger UI",
                oauth2_redirect_url=app.swagger_ui_oauth2_redirect_url,
                swagger_js_url="/static/docs-assets/swagger-ui-bundle.js",
                swagger_css_url="/static/docs-assets/swagger-ui.css",
                swagger_favicon_url="/static/docs-assets/favicon.png",
            )

        @app.get("/redoc", include_in_schema=False)
        async def redoc_html():
            return get_redoc_html(
                openapi_url=app.openapi_url,  # type: ignore
                title=app.title + " - ReDoc",
                redoc_js_url="/static/docs-assets/redoc.standalone.js",
                redoc_favicon_url="/static/docs-assets/favicon.png",
            )

    # ── Routes ─────────────────────────────────────────────────────────────────
    # Standard OAuth2 token endpoint at root /token.
    # Must return {access_token, token_type} at the TOP LEVEL so that
    # Swagger UI can extract the token and auto-fill Authorization headers.
    @app.post(
        "/token",
        response_model=OAuth2TokenResponse,
        tags=["auth"],
        summary="Login — obtain a JWT access token",
    )
    async def token_endpoint(
        response: Response,
        form_data: OAuth2PasswordRequestForm = Depends(),
    ) -> OAuth2TokenResponse:
        svc = AuthService(JsonUserRepository(settings.USERS_JSON_PATH))
        user = await svc.authenticate(form_data.username, form_data.password)
        token_data = await svc.generate_token(user)
        # Also set the JWT as an HttpOnly, same-origin cookie so the HTML log
        # viewers (which cannot attach a Bearer header to their fetch() polls)
        # are authenticated. NOTE: Swagger's /token call is an XHR whose
        # Set-Cookie the browser does NOT persist — for a browser-first login
        # use GET/POST /login below. API/Swagger callers keep using the
        # access_token from the response body as a Bearer header.
        set_access_cookie(response, token_data.access_token, token_data.expires_in)
        return OAuth2TokenResponse(
            access_token=token_data.access_token,
            token_type=token_data.token_type,
            expires_in=token_data.expires_in,
        )

    # Browser-first login: a real same-origin form whose POST sets the cookie
    # and redirects to `next`. This is what makes the HTML log viewer usable by
    # just opening a URL (Swagger's XHR /token cannot persist the cookie).
    @app.get(
        "/login",
        response_class=HTMLResponse,
        tags=["auth"],
        summary="Login page (browser)",
    )
    # `next` is the query key; aliased so the builtin is not shadowed.
    async def login_page(next_url: str = Query("/docs", alias="next"), error: str = ""):
        safe_next = html.escape(safe_next_path(next_url), quote=True)
        error_html = f'<div class="error">{html.escape(error)}</div>' if error else ""
        return LOGIN_HTML.format(next=safe_next, error=error_html)

    @app.post("/login", tags=["auth"], summary="Login form submit (browser)")
    async def login_submit(
        response: Response,
        form_data: OAuth2PasswordRequestForm = Depends(),
        next_url: str = Form("/docs", alias="next"),
    ):
        target = safe_next_path(next_url)
        svc = AuthService(JsonUserRepository(settings.USERS_JSON_PATH))
        try:
            user = await svc.authenticate(form_data.username, form_data.password)
        except AuthException:
            # Re-render the form with an error (no cookie set).
            safe_next = html.escape(target, quote=True)
            body = LOGIN_HTML.format(
                next=safe_next,
                error='<div class="error">Invalid username or password.</div>',
            )
            return HTMLResponse(body, status_code=200)
        token_data = await svc.generate_token(user)
        redirect = RedirectResponse(url=target, status_code=303)
        set_access_cookie(redirect, token_data.access_token, token_data.expires_in)
        return redirect

    app.include_router(api_router)

    Instrumentator().instrument(app).expose(app)

    return app


app = create_app()
