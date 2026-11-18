from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request

from app.administration import service
from app.administration.schemas import (
    RoleChange,
    RoleChangeResult,
    RoleFilter,
    UserPage,
    UserProfile,
)
from app.audit.schemas import AuditPage
from app.audit.service import list_user_events
from app.auth_dependencies import Db, require_roles
from app.auth_protection import check_csrf, rate_limit
from app.models import User
from app.videos.router import ERRORS, PageCursor, PageLimit

router = APIRouter(prefix="/api/admin", tags=["administration"])
Admin = Annotated[User, Depends(require_roles("admin"))]
SearchQuery = Annotated[str, Query(max_length=200)]
UserRoleFilter = Annotated[RoleFilter, Query(alias="role")]


def admin_mutation(user: Admin, _csrf: Annotated[None, Depends(check_csrf)]):
    return user


AdminWriter = Annotated[User, Depends(admin_mutation)]


@router.get("/users", response_model=UserPage, responses=ERRORS)
def users(
    db: Db,
    user: Admin,
    q: SearchQuery = "",
    role: UserRoleFilter = "all",
    limit: PageLimit = 20,
    cursor: PageCursor = None,
):
    return service.list_users(db, user.id, q, role, limit, cursor)


@router.get("/users/{user_id}", response_model=UserProfile, responses=ERRORS)
def detail(user_id: UUID, db: Db, user: Admin):
    return service.profile(db, user_id)


@router.patch("/users/{user_id}/role", response_model=RoleChangeResult, responses=ERRORS)
def role(user_id: UUID, payload: RoleChange, request: Request, db: Db, user: AdminWriter):
    rate_limit(
        db,
        f"administration:write:{user.id}",
        request.app.state.settings.admin_write_limit_per_minute,
        60,
    )
    return service.change_role(db, user.id, user_id, payload)


@router.get("/audit", response_model=AuditPage, responses=ERRORS)
def audit(
    db: Db,
    user: Admin,
    user_id: UUID | None = None,
    limit: PageLimit = 20,
    cursor: PageCursor = None,
):
    return list_user_events(db, user.id, user_id, limit, cursor)
