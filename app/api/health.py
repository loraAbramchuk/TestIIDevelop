import logging
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Literal

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.models import AuthRateLimit, AuthSession, User

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/health", tags=["health"])


class LiveStatus(BaseModel):
    status: Literal["ok"] = "ok"


class ReadyStatus(BaseModel):
    status: Literal["ok", "unavailable"]
    checks: dict[str, Literal["ok", "error"]]


def check_storage(root: Path) -> None:
    # Реальная запись, а не только проверка прав: обнаруживает read-only mount/нет места.
    with NamedTemporaryFile(dir=root, prefix=".health-") as probe:
        probe.write(b"health")
        probe.flush()


@router.get("/live", response_model=LiveStatus)
def live() -> LiveStatus:
    return LiveStatus()


@router.get("/ready", response_model=ReadyStatus, responses={503: {"model": ReadyStatus}})
def ready(request: Request, response: Response) -> ReadyStatus:
    checks = {}
    try:
        with request.app.state.session_factory() as session:
            # Проверяет не только соединение, но и наличие прикладной схемы после миграций.
            session.execute(select(User.id).limit(1))
            session.execute(select(AuthSession.token_hash).limit(1))
            session.execute(select(AuthRateLimit.key).limit(1))
        checks["database"] = "ok"
    except SQLAlchemyError:
        logger.warning("Database readiness check failed")
        checks["database"] = "error"
    try:
        check_storage(request.app.state.settings.media_root)
        checks["storage"] = "ok"
    except OSError:
        logger.warning("Storage readiness check failed")
        checks["storage"] = "error"
    healthy = all(value == "ok" for value in checks.values())
    response.status_code = 200 if healthy else 503
    response.headers["Cache-Control"] = "no-store"
    return ReadyStatus(status="ok" if healthy else "unavailable", checks=checks)
