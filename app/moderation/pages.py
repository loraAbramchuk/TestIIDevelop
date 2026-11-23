from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.audit.service import list_events
from app.auth_dependencies import Db
from app.catalog.pages import error_page, render
from app.errors import DomainError
from app.moderation import service
from app.moderation.router import StatusFilter
from app.navigation import login_redirect
from app.videos.pages import PageUser
from app.videos.router import PageCursor, PageLimit

router = APIRouter(include_in_schema=False)


def access(request, user):
    if user is None:
        return login_redirect(request)
    if user.role not in ("moderator", "admin"):
        return error_page(request, DomainError(403, "forbidden", "Недостаточно прав"), user)


@router.get("/moderation", response_class=HTMLResponse)
def queue(
    request: Request,
    db: Db,
    user: PageUser,
    status: StatusFilter = "open",
    limit: PageLimit = 20,
    cursor: PageCursor = None,
):
    if denied := access(request, user):
        return denied
    try:
        return render(
            request,
            "moderation_queue.html",
            user,
            page=service.list_reports(db, user.id, status, limit, cursor),
            status_filter=status,
            limit=limit,
        )
    except DomainError as error:
        return error_page(request, error, user)


@router.get("/moderation/reports/{report_id}", response_class=HTMLResponse)
def report(report_id: UUID, request: Request, db: Db, user: PageUser):
    if denied := access(request, user):
        return denied
    try:
        return render(
            request,
            "moderation_report.html",
            user,
            report=service.report_profile(db, report_id, user.id),
        )
    except DomainError as error:
        return error_page(request, error, user)


@router.get("/moderation/videos/{video_id}", response_class=HTMLResponse)
def video(video_id: UUID, request: Request, db: Db, user: PageUser):
    if denied := access(request, user):
        return denied
    try:
        return render(
            request,
            "moderation_video.html",
            user,
            video=service.video_profile(db, video_id, user.id),
        )
    except DomainError as error:
        return error_page(request, error, user)


@router.get("/moderation/audit", response_class=HTMLResponse)
def audit(
    request: Request,
    db: Db,
    user: PageUser,
    video_id: UUID | None = None,
    limit: PageLimit = 20,
    cursor: PageCursor = None,
):
    if denied := access(request, user):
        return denied
    try:
        return render(
            request,
            "moderation_audit.html",
            user,
            page=list_events(db, user.id, video_id, limit, cursor),
            video_id=video_id,
            limit=limit,
        )
    except DomainError as error:
        return error_page(request, error, user)
