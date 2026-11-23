import hashlib

from fastapi import HTTPException
from sqlalchemy import func, or_, select, tuple_

from app.administration.schemas import AdminUser, RoleChangeResult, UserPage, UserProfile
from app.audit.service import record
from app.errors import DomainError
from app.models import User
from app.pagination import decode_cursor, encode_cursor
from app.text_validation import validate_text

# One transaction-level mutex for every API role writer, before any user row lock.
ROLE_LOCK_NAMESPACE = 0x56494445
ROLE_LOCK_KEY = 10


def normalize_query(value):
    try:
        return validate_text(value).strip()
    except ValueError:
        raise DomainError(400, "invalid_query", "Недопустимый поисковый запрос") from None


def admin_count(db):
    return db.scalar(select(func.count()).select_from(User).where(User.role == "admin"))


def list_users(db, viewer_id, query, role, limit, cursor):
    query = normalize_query(query)
    scope = f"users:{viewer_id}:{role}:{hashlib.sha256(query.encode()).hexdigest()}"
    position = decode_cursor(cursor, scope)
    statement = select(User)
    if query:
        statement = statement.where(
            or_(
                User.email.icontains(query, autoescape=True),
                User.display_name.icontains(query, autoescape=True),
            )
        )
    if role != "all":
        statement = statement.where(User.role == role)
    if position:
        statement = statement.where(tuple_(User.created_at, User.id) < position)
    rows = list(
        db.scalars(statement.order_by(User.created_at.desc(), User.id.desc()).limit(limit + 1))
    )
    page = rows[:limit]
    return UserPage(
        items=[AdminUser.model_validate(row) for row in page],
        admin_count=admin_count(db),
        next_cursor=encode_cursor(scope, page[-1].created_at, page[-1].id)
        if len(rows) > limit
        else None,
    )


def profile(db, user_id):
    user = db.get(User, user_id)
    if user is None:
        raise DomainError(404, "user_not_found", "Пользователь не найден")
    return UserProfile(user=AdminUser.model_validate(user), admin_count=admin_count(db))


def change_role(db, actor_id, user_id, payload):
    db.execute(select(func.pg_advisory_xact_lock(ROLE_LOCK_NAMESPACE, ROLE_LOCK_KEY)))
    actor = db.scalar(
        select(User)
        .where(User.id == actor_id)
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    )
    if actor is None or actor.role != "admin":
        raise HTTPException(403, "Недостаточно прав")
    # Role is not a key: NO KEY UPDATE also permits unrelated FK inserts.
    user = db.scalar(
        select(User)
        .where(User.id == user_id)
        .with_for_update(key_share=True)
        .execution_options(populate_existing=True)
    )
    if user is None:
        raise DomainError(404, "user_not_found", "Пользователь не найден")
    changed = user.role != payload.role
    if changed and payload.expected_role is not None and user.role != payload.expected_role:
        raise DomainError(
            409, "role_conflict", "Роль уже изменена. Обновите сведения перед новым решением."
        )
    if changed and user.role == "admin" and payload.role != "admin" and admin_count(db) <= 1:
        raise DomainError(409, "last_admin", "Нельзя понизить роль последнего администратора")
    if changed:
        previous = user.role
        user.role = payload.role
        record(
            db,
            actor_id,
            "user.role_changed",
            "user",
            user_id,
            payload.reason,
            previous_role=previous,
            role=payload.role,
        )
    db.flush()
    result = RoleChangeResult(
        user=AdminUser.model_validate(user),
        changed=changed,
        admin_count=admin_count(db),
        actor_role=actor.role,
    )
    db.commit()
    return result
