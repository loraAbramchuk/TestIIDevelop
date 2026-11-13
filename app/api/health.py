import logging
from shutil import disk_usage
from typing import Literal

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.models import AuthRateLimit, AuthSession, User
from app.storage_health import check_storage_read, check_storage_write

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/health", tags=["health"])


class LiveStatus(BaseModel):
    status: Literal["ok"] = "ok"


class ReadyStatus(BaseModel):
    status: Literal["ok", "unavailable"]
    checks: dict[str, Literal["ok", "error"]]


class Diagnostics(BaseModel):
    status: Literal["ok", "degraded", "unavailable"]
    checks: dict[str, Literal["ok", "error", "low", "not_configured"]]
    video_uploads: Literal["not_implemented"] = "not_implemented"


def check_database(request: Request) -> None:
    with request.app.state.session_factory() as session:
        session.execute(select(User.id).limit(1))
        session.execute(select(AuthSession.token_hash).limit(1))
        session.execute(select(AuthRateLimit.key).limit(1))


def readiness_checks(request: Request) -> dict:
    checks = {}
    try:
        check_database(request)
        checks["database"] = "ok"
    except SQLAlchemyError:
        logger.warning("Database readiness check failed")
        checks["database"] = "error"
    try:
        check_storage_read(request.app.state.settings.media_root)
        checks["storage"] = "ok"
    except OSError:
        logger.warning("Storage read readiness check failed")
        checks["storage"] = "error"
    return checks


@router.get("/live", response_model=LiveStatus)
def live() -> LiveStatus:
    return LiveStatus()


@router.get("/ready", response_model=ReadyStatus, responses={503: {"model": ReadyStatus}})
def ready(request: Request, response: Response) -> ReadyStatus:
    checks = readiness_checks(request)
    healthy = all(value == "ok" for value in checks.values())
    response.status_code = 200 if healthy else 503
    response.headers["Cache-Control"] = "no-store"
    return ReadyStatus(status="ok" if healthy else "unavailable", checks=checks)


@router.get("/diagnostics", response_model=Diagnostics)
def diagnostics(request: Request, response: Response) -> Diagnostics:
    checks = readiness_checks(request)
    settings = request.app.state.settings
    try:
        check_storage_write(settings.media_root)
        checks["storage_write"] = "ok"
    except OSError:
        checks["storage_write"] = "error"
    try:
        checks["disk_reserve"] = (
            "ok" if disk_usage(settings.media_root).free >= settings.media_min_free_bytes else "low"
        )
    except OSError:
        checks["disk_reserve"] = "error"
    # No worker exists yet. A future processing adapter must report its actual heartbeat.
    checks["processing"] = "not_configured"
    status = "ok"
    if checks["database"] == "error" or checks["storage"] == "error":
        status = "unavailable"
    elif checks["storage_write"] == "error" or checks["disk_reserve"] != "ok":
        status = "degraded"
    response.headers["Cache-Control"] = "no-store"
    return Diagnostics(status=status, checks=checks)
