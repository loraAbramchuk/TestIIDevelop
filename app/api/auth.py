import hmac
import math
import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from sqlalchemy import case, delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError

from app.auth_dependencies import (
    CurrentSession,
    CurrentUser,
    Db,
    session_token,
)
from app.auth_schemas import AuthOutput, CsrfOutput, LoginInput, RegisterInput, UserOutput
from app.models import AuthRateLimit, AuthSession, User
from app.security import csrf_token, hash_password, password_hasher, token_hash, verify_password

router = APIRouter(prefix="/api/auth", tags=["auth"])


def rate_limit(db, key: str, limit: int, seconds: int) -> None:
    now = datetime.now(UTC)
    statement = insert(AuthRateLimit).values(
        key=token_hash(key), attempts=1, resets_at=now + timedelta(seconds=seconds)
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
    # Failed logins must consume attempts too; the counter is shared across workers.
    db.commit()
    if attempts > limit:
        wait = max(1, math.ceil((resets_at - now).total_seconds()))
        raise HTTPException(
            429, "Слишком много попыток. Попробуйте позже", headers={"Retry-After": str(wait)}
        )


def peer(request: Request) -> str:
    return request.client.host if request.client else "unknown"


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


def set_session_cookie(request: Request, response: Response, token: str, seconds: int) -> None:
    settings = request.app.state.settings
    response.set_cookie(
        settings.session_cookie_name,
        token,
        max_age=seconds,
        httponly=True,
        secure=settings.auth_cookie_secure,
        samesite="lax",
        path="/",
    )


def new_session(db, user_id, seconds: int) -> str:
    token = secrets.token_urlsafe(32)
    db.add(
        AuthSession(
            token_hash=token_hash(token),
            user_id=user_id,
            expires_at=datetime.now(UTC) + timedelta(seconds=seconds),
        )
    )
    return token


def authenticated_response(request, response, db, previous, user) -> AuthOutput:
    if previous:
        db.delete(previous)
    seconds = request.app.state.settings.session_lifetime_hours * 3600
    token = new_session(db, user.id, seconds)
    db.commit()
    set_session_cookie(request, response, token, seconds)
    return AuthOutput(user=UserOutput.model_validate(user), csrf_token=csrf_token(token))


@router.get("/csrf", response_model=CsrfOutput, responses={429: {"description": "Лимит запросов"}})
def get_csrf(request: Request, response: Response, db: Db, session: CurrentSession) -> CsrfOutput:
    if session:
        return CsrfOutput(csrf_token=csrf_token(session_token(request)))
    rate_limit(db, f"csrf:{peer(request)}", 60, 60)
    # Bound persistent state from expired anonymous/authenticated sessions and old counters.
    now = datetime.now(UTC)
    db.execute(delete(AuthSession).where(AuthSession.expires_at <= now))
    db.execute(delete(AuthRateLimit).where(AuthRateLimit.resets_at < now))
    token = new_session(db, None, 1800)
    db.commit()
    set_session_cookie(request, response, token, 1800)
    return CsrfOutput(csrf_token=csrf_token(token))


@router.post(
    "/register",
    response_model=AuthOutput,
    status_code=201,
    dependencies=[Depends(check_csrf)],
    responses={
        403: {"description": "Ошибка CSRF"},
        409: {"description": "Email занят"},
        429: {"description": "Лимит запросов"},
    },
)
def register(
    payload: RegisterInput, request: Request, response: Response, db: Db, session: CurrentSession
):
    rate_limit(db, f"register:{peer(request)}", 5, 3600)
    if session.user_id:
        raise HTTPException(409, "Сначала выйдите из текущего аккаунта")
    user = User(
        email=str(payload.email),
        display_name=payload.display_name,
        password_hash=hash_password(payload.password.get_secret_value()),
        role="user",
    )
    db.add(user)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Не удалось зарегистрироваться с этим email") from None
    return authenticated_response(request, response, db, session, user)


@router.post(
    "/login",
    response_model=AuthOutput,
    dependencies=[Depends(check_csrf)],
    responses={
        401: {"description": "Неверные данные"},
        403: {"description": "Ошибка CSRF"},
        429: {"description": "Лимит запросов"},
    },
)
def login(
    payload: LoginInput, request: Request, response: Response, db: Db, session: CurrentSession
):
    rate_limit(db, f"login-ip:{peer(request)}", 30, 900)
    rate_limit(db, f"login-email:{payload.email}", 10, 900)
    user = db.scalar(select(User).where(User.email == str(payload.email)))
    verified = verify_password(
        payload.password.get_secret_value(), user.password_hash if user else None
    )
    if not verified or user is None:
        raise HTTPException(401, "Неверный email или пароль")
    if password_hasher.check_needs_rehash(user.password_hash):
        user.password_hash = hash_password(payload.password.get_secret_value())
    return authenticated_response(request, response, db, session, user)


@router.get("/me", response_model=UserOutput, responses={401: {"description": "Требуется вход"}})
def me(user: CurrentUser) -> User:
    return user


@router.post("/logout", status_code=204, dependencies=[Depends(check_csrf)])
def logout(request: Request, response: Response, db: Db, session: CurrentSession):
    db.delete(session)
    db.commit()
    response.delete_cookie(
        request.app.state.settings.session_cookie_name,
        path="/",
        secure=request.app.state.settings.auth_cookie_secure,
        httponly=True,
        samesite="lax",
    )
