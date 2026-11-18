from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response

from app.audit.service import list_events
from app.auth_dependencies import Db, require_roles
from app.auth_protection import check_csrf, rate_limit
from app.models import User
from app.moderation import service
from app.moderation.schemas import (
    AuditPage,
    ModerationVideo,
    ReportCreate,
    ReportDecision,
    ReportFilter,
    ReportPage,
    ReportReceipt,
    StaffReport,
    VideoModeration,
)
from app.videos.router import ERRORS, PageCursor, PageLimit, Writer

router = APIRouter(tags=["moderation"])
Staff = Annotated[User, Depends(require_roles("moderator", "admin"))]


def staff_mutation(user: Staff, _csrf: Annotated[None, Depends(check_csrf)]):
    return user


StaffWriter = Annotated[User, Depends(staff_mutation)]
StatusFilter = Annotated[ReportFilter, Query(alias="status")]


@router.post(
    "/api/videos/{video_id}/reports",
    response_model=ReportReceipt,
    status_code=201,
    responses=ERRORS | {200: {"model": ReportReceipt, "description": "Уже есть открытая жалоба"}},
)
def report(
    video_id: UUID,
    payload: ReportCreate,
    response: Response,
    request: Request,
    db: Db,
    user: Writer,
):
    rate_limit(
        db,
        f"reports:create:{user.id}",
        request.app.state.settings.report_create_limit_per_hour,
        3600,
    )
    result, created = service.submit_report(db, video_id, user.id, payload)
    response.status_code = 201 if created else 200
    return result


def write_limit(db, request, user):
    rate_limit(
        db,
        f"moderation:write:{user.id}",
        request.app.state.settings.moderation_write_limit_per_minute,
        60,
    )


@router.get("/api/moderation/reports", response_model=ReportPage, responses=ERRORS)
def reports(
    db: Db,
    user: Staff,
    status: StatusFilter = "open",
    limit: PageLimit = 20,
    cursor: PageCursor = None,
):
    return service.list_reports(db, user.id, status, limit, cursor)


@router.get("/api/moderation/reports/{report_id}", response_model=StaffReport, responses=ERRORS)
def report_detail(report_id: UUID, db: Db, user: Staff):
    return service.report_profile(db, report_id, user.id)


@router.post(
    "/api/moderation/reports/{report_id}/decision", response_model=StaffReport, responses=ERRORS
)
def decision(report_id: UUID, payload: ReportDecision, request: Request, db: Db, user: StaffWriter):
    write_limit(db, request, user)
    return service.decide(db, report_id, user.id, payload)


@router.get("/api/moderation/videos/{video_id}", response_model=ModerationVideo, responses=ERRORS)
def video(video_id: UUID, db: Db, user: Staff):
    return service.video_profile(db, video_id, user.id)


@router.post(
    "/api/moderation/videos/{video_id}/visibility", response_model=ModerationVideo, responses=ERRORS
)
def visibility(
    video_id: UUID, payload: VideoModeration, request: Request, db: Db, user: StaffWriter
):
    write_limit(db, request, user)
    return service.moderate_video(db, video_id, user.id, payload)


@router.get("/api/moderation/audit", response_model=AuditPage, responses=ERRORS)
def audit(
    db: Db,
    user: Staff,
    video_id: UUID | None = None,
    limit: PageLimit = 20,
    cursor: PageCursor = None,
):
    return list_events(db, user.id, video_id, limit, cursor)
