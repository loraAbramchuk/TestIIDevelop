import re
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from fastapi.security import APIKeyCookie
from sqlalchemy.orm import Session

from app.models import AuthSession, User
from app.security import token_hash


def get_db(request: Request) -> Iterator[Session]:
    with request.app.state.session_factory() as db:
        yield db


Db = Annotated[Session, Depends(get_db)]


def session_token(request: Request) -> str | None:
    value = request.cookies.get(request.app.state.settings.session_cookie_name, "")
    return value if re.fullmatch(r"[A-Za-z0-9_-]{43}", value) else None


def get_session(request: Request, db: Db) -> AuthSession | None:
    token = session_token(request)
    session = db.get(AuthSession, token_hash(token)) if token else None
    if session is None or session.expires_at <= datetime.now(UTC):
        return None
    return session


CurrentSession = Annotated[AuthSession | None, Depends(get_session)]


def optional_user(db: Db, session: CurrentSession) -> User | None:
    return db.get(User, session.user_id) if session and session.user_id else None


# Document cookie authentication; the actual name depends on the HTTPS setting.
cookie_scheme = APIKeyCookie(name="video_session", auto_error=False)


def current_user(
    db: Db,
    session: CurrentSession,
    _cookie: Annotated[str | None, Depends(cookie_scheme)],
) -> User:
    user = db.get(User, session.user_id) if session and session.user_id else None
    if user is None:
        raise HTTPException(401, "Требуется вход в аккаунт")
    return user


CurrentUser = Annotated[User, Depends(current_user)]


def require_roles(*roles: str):
    def check(user: CurrentUser) -> User:
        # The user is loaded from the database on each request; role changes apply immediately.
        if user.role not in roles:
            raise HTTPException(403, "Недостаточно прав")
        return user

    return check
