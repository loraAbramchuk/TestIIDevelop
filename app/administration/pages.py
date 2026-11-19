from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.administration import service
from app.administration.router import SearchQuery, UserRoleFilter
from app.audit.service import list_user_events
from app.auth_dependencies import Db
from app.catalog.pages import error_page, render
from app.errors import DomainError
from app.navigation import login_redirect
from app.videos.pages import PageUser
from app.videos.router import PageCursor, PageLimit

router = APIRouter(include_in_schema=False)


def access(request, user):
    if user is None:
        return login_redirect(request)
    if user.role != "admin":
        return error_page(request, DomainError(403, "forbidden", "Недостаточно прав"), user)


@router.get("/admin/users", response_class=HTMLResponse)
def users(
    request: Request,
    db: Db,
    user: PageUser,
    q: SearchQuery = "",
    role: UserRoleFilter = "all",
    limit: PageLimit = 20,
    cursor: PageCursor = None,
):
    if denied := access(request, user):
        return denied
    try:
        return render(
            request,
            "admin_users.html",
            user,
            page=service.list_users(db, user.id, q, role, limit, cursor),
            user_query=q,
            role_filter=role,
            limit=limit,
        )
    except DomainError as error:
        return error_page(request, error, user)


@router.get("/admin/audit", response_class=HTMLResponse)
def audit(
    request: Request,
    db: Db,
    user: PageUser,
    user_id: UUID | None = None,
    limit: PageLimit = 20,
    cursor: PageCursor = None,
):
    if denied := access(request, user):
        return denied
    try:
        return render(
            request,
            "admin_audit.html",
            user,
            page=list_user_events(db, user.id, user_id, limit, cursor),
            target_user_id=user_id,
            limit=limit,
        )
    except DomainError as error:
        return error_page(request, error, user)
