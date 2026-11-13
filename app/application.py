from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.api.auth import router as auth_router
from app.api.health import router as health_router
from app.auth_dependencies import optional_user
from app.config import Settings
from app.db import make_engine, make_session_factory
from app.models import User
from app.storage_health import initialize_storage

PACKAGE_ROOT = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=PACKAGE_ROOT / "templates")


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
        version="0.1.0",
        description=(
            "API видеоплатформы. Авторизация: сначала GET /api/auth/csrf, затем "
            "передайте csrf_token в X-CSRF-Token при регистрации, входе и выходе. "
            "Сессия хранится в HttpOnly cookie."
        ),
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = make_session_factory(engine)
    app.include_router(health_router)
    app.include_router(auth_router)
    app.mount("/static", StaticFiles(directory=PACKAGE_ROOT / "static"), name="static")

    original_openapi = app.openapi

    def openapi():
        schema = original_openapi()
        schema["components"]["securitySchemes"]["APIKeyCookie"]["name"] = (
            settings.session_cookie_name
        )
        return schema

    app.openapi = openapi

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        # Never echo passwords/other invalid input in validation errors.
        errors = [{"loc": e["loc"], "msg": e["msg"], "type": e["type"]} for e in exc.errors()]
        return JSONResponse(status_code=422, content={"detail": errors})

    @app.middleware("http")
    async def response_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        if request.url.path.startswith("/api/auth") or request.url.path in (
            "/",
            "/login",
            "/register",
        ):
            response.headers["Cache-Control"] = "no-store"
            response.headers.append("Vary", "Cookie")
        return response

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def home(request: Request, user: Annotated[User | None, Depends(optional_user)]):
        return templates.TemplateResponse(
            request=request, name="home.html", context={"app_name": settings.app_name, "user": user}
        )

    @app.get("/login", response_class=HTMLResponse, include_in_schema=False)
    @app.get("/register", response_class=HTMLResponse, include_in_schema=False)
    def auth_page(request: Request, user: Annotated[User | None, Depends(optional_user)]):
        if user:
            return RedirectResponse("/", status_code=303)
        return templates.TemplateResponse(
            request=request,
            name="auth.html",
            context={
                "app_name": settings.app_name,
                "user": None,
                "register": request.url.path == "/register",
            },
        )

    return app
