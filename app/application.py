from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import SQLAlchemyError

from app import __version__
from app.administration.pages import router as admin_pages
from app.administration.router import router as admin_router
from app.api.auth import router as auth_router
from app.api.health import router as health_router
from app.auth_dependencies import optional_user
from app.catalog.pages import error_page
from app.catalog.pages import router as catalog_pages
from app.catalog.router import router as catalog_router
from app.channels.router import router as channel_router
from app.config import Settings
from app.db import make_engine, make_session_factory
from app.errors import DomainError
from app.interactions.router import router as interaction_router
from app.mail.router import router as mail_router
from app.models import User
from app.moderation.pages import router as moderation_pages
from app.moderation.router import router as moderation_router
from app.navigation import local_return_path
from app.playlists.pages import router as playlist_pages
from app.playlists.router import router as playlist_router
from app.storage_health import initialize_storage
from app.templating import PACKAGE_ROOT, static_url, templates
from app.uploads.router import router as upload_router
from app.videos.pages import router as video_pages
from app.videos.router import router as video_router
from app.videos.service import VideoError


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    engine = make_engine(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        initialize_storage(settings.media_root)
        try:
            yield
        finally:
            engine.dispose()

    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        docs_url=None,
        description=(
            "API видеоплатформы. Авторизация: сначала GET /api/auth/csrf, затем "
            "передайте csrf_token в X-CSRF-Token при регистрации, входе и выходе. "
            "Сессия хранится в HttpOnly cookie."
            " Новые аккаунты подтверждают email ссылкой из письма перед изменением контента. "
            "Восстановление: POST /api/auth/password/request и одноразовая ссылка из письма; "
            "после смены пароля прежние сеансы завершаются."
        ),
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = make_session_factory(engine)
    app.include_router(admin_router)
    app.include_router(admin_pages)
    app.include_router(health_router)
    app.include_router(auth_router)
    app.include_router(mail_router)
    app.include_router(video_router)
    app.include_router(upload_router)
    app.include_router(video_pages)
    app.include_router(catalog_router)
    app.include_router(channel_router)
    app.include_router(interaction_router)
    app.include_router(playlist_router)
    app.include_router(playlist_pages)
    app.include_router(moderation_router)
    app.include_router(moderation_pages)
    app.include_router(catalog_pages)
    app.mount("/static", StaticFiles(directory=PACKAGE_ROOT / "static"), name="static")

    @app.get("/docs", include_in_schema=False)
    def swagger_ui(request: Request):
        assets = "/vendor/swagger-ui/5.35.0/"
        return get_swagger_ui_html(
            openapi_url=request.url_for("openapi").path,
            title=f"{settings.app_name} — Swagger UI",
            swagger_js_url=static_url(request, assets + "swagger-ui-bundle.js"),
            swagger_css_url=static_url(request, assets + "swagger-ui.css"),
            swagger_favicon_url=static_url(request, assets + "favicon-32x32.png"),
            swagger_ui_parameters={"validatorUrl": None},
        )

    original_openapi = app.openapi

    def openapi():
        schema = original_openapi()
        schema["components"]["securitySchemes"]["APIKeyCookie"]["name"] = (
            settings.session_cookie_name
        )
        return schema

    app.openapi = openapi

    @app.exception_handler(VideoError)
    async def video_error(request: Request, exc: VideoError):
        if not request.url.path.startswith("/api/"):
            return error_page(request, exc)
        return JSONResponse(
            status_code=exc.status,
            content={"detail": exc.message, "code": exc.code},
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        if request.url.path in (
            "/",
            "/search",
            "/subscriptions",
            "/my-playlists",
            "/moderation",
            "/studio",
        ) or request.url.path.startswith(
            ("/channels/", "/playlists/", "/moderation/", "/admin/", "/studio/", "/watch/")
        ):
            return error_page(
                request,
                DomainError(
                    422,
                    "invalid_request",
                    "Проверьте ссылку и параметры страницы или сократите поисковый запрос.",
                ),
            )
        # Never echo passwords/other invalid input in validation errors.
        errors = [{"loc": e["loc"], "msg": e["msg"], "type": e["type"]} for e in exc.errors()]
        return JSONResponse(status_code=422, content={"detail": errors})

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request: Request, exc: SQLAlchemyError):
        error = DomainError(
            503, "service_unavailable", "Сервис временно недоступен. Попробуйте позже."
        )
        if not request.url.path.startswith("/api/"):
            return error_page(request, error)
        return JSONResponse(
            status_code=503,
            content={"detail": error.message, "code": error.code},
            headers={"Cache-Control": "private, no-store"},
        )

    @app.middleware("http")
    async def response_headers(request: Request, call_next):
        route_path = request.url.path.removeprefix(request.scope.get("root_path", ""))
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        if route_path.startswith("/static/"):
            # URLs change with content; revalidate reused URLs instead of relying
            # on a heuristic freshness lifetime inherited from Last-Modified.
            response.headers["Cache-Control"] = "no-cache"
        if request.url.path in ("/verify-email", "/forgot-password", "/reset-password"):
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["X-Robots-Tag"] = "noindex, nofollow"
        if request.url.path.startswith(
            (
                "/api/videos",
                "/api/studio/videos",
                "/studio",
                "/upload",
                "/watch/",
                "/api/channels",
                "/api/subscriptions",
                "/channels",
                "/subscriptions",
                "/my-channel",
                "/search",
                "/api/playlists",
                "/playlists",
                "/my-playlists",
                "/api/admin",
                "/admin/",
                "/api/moderation",
                "/moderation",
            )
        ):
            response.headers["Cache-Control"] = "private, no-store"
            response.headers.append("Vary", "Cookie")
        elif request.url.path.startswith("/api/auth") or request.url.path in (
            "/",
            "/login",
            "/register",
            "/verify-email",
            "/forgot-password",
            "/reset-password",
        ):
            response.headers["Cache-Control"] = "no-store"
            response.headers.append("Vary", "Cookie")
        return response

    @app.get("/login", response_class=HTMLResponse, include_in_schema=False)
    @app.get("/register", response_class=HTMLResponse, include_in_schema=False)
    def auth_page(request: Request, user: Annotated[User | None, Depends(optional_user)]):
        next_path = local_return_path(request.query_params.get("next"))
        if user:
            return RedirectResponse(next_path, status_code=303)
        return templates.TemplateResponse(
            request=request,
            name="auth.html",
            context={
                "app_name": settings.app_name,
                "user": None,
                "register": request.url.path == "/register",
                "next_path": next_path,
            },
        )

    @app.get("/verify-email", response_class=HTMLResponse, include_in_schema=False)
    @app.get("/forgot-password", response_class=HTMLResponse, include_in_schema=False)
    @app.get("/reset-password", response_class=HTMLResponse, include_in_schema=False)
    def mail_page(request: Request, user: Annotated[User | None, Depends(optional_user)]):
        return templates.TemplateResponse(
            request=request,
            name="auth_mail.html",
            context={"app_name": settings.app_name, "user": user, "action": request.url.path[1:]},
        )

    return app
