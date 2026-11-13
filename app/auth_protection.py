"""Shared CSRF and persistent throttling for state-changing endpoints."""

import hmac
import math
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import Header, HTTPException, Request
from sqlalchemy import case
from sqlalchemy.dialects.postgresql import insert

from app.auth_dependencies import CurrentSession, session_token
from app.models import AuthRateLimit
from app.security import csrf_token, token_hash


def rate_limit(db, key: str, limit: int, seconds: int) -> None:
    now = datetime.now(UTC)
    statement = insert(AuthRateLimit).values(
        key=token_hash(key),
        attempts=1,
        resets_at=now + timedelta(seconds=seconds),
    )
    expired = AuthRateLimit.resets_at <= now
    statement = statement.on_conflict_do_update(
        index_elements=[AuthRateLimit.key],
        set_={
            "attempts": case((expired, 1), else_=AuthRateLimit.attempts + 1),
            "resets_at": case(
                (expired, statement.excluded.resets_at), else_=AuthRateLimit.resets_at
            ),
        },
    ).returning(AuthRateLimit.attempts, AuthRateLimit.resets_at)
    attempts, resets_at = db.execute(statement).one()
    # Called before taking domain locks. Counters survive failed operations.
    db.commit()
    if attempts > limit:
        wait = max(1, math.ceil((resets_at - now).total_seconds()))
        raise HTTPException(
            429, "Слишком много попыток. Попробуйте позже", headers={"Retry-After": str(wait)}
        )


def check_csrf(
    request: Request,
    session: CurrentSession,
    header: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> None:
    token = session_token(request)
    if (
        not session
        or not token
        or not header
        or not hmac.compare_digest(csrf_token(token).encode(), header.encode())
    ):
        raise HTTPException(403, "Недействительный CSRF-токен. Обновите страницу")
