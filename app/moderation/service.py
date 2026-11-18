from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import select, tuple_

from app.audit.service import record
from app.errors import DomainError
from app.models import Report, User, Video
from app.moderation.schemas import ModerationVideo, ReportPage, ReportReceipt, StaffReport
from app.pagination import decode_cursor, encode_cursor
from app.videos.access import viewable_by
from app.videos.service import visible_record


def staff_actor(db, user_id):
    # Recheck after waiting; the role cannot change until the audited action commits.
    user = db.scalar(
        select(User)
        .where(User.id == user_id)
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    )
    if user is None or user.role not in ("moderator", "admin"):
        raise HTTPException(403, "Недостаточно прав")


def locked_video(db, identifier):
    video = db.scalar(
        select(Video)
        .where(Video.id == identifier)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if video is None:
        raise DomainError(404, "video_not_found", "Видео не найдено")
    return video


def submit_report(db, video_id, user_id, payload):
    locked_video(db, video_id)
    visible_record(db, video_id, user_id)
    existing = db.scalar(
        select(Report).where(
            Report.video_id == video_id, Report.reporter_id == user_id, Report.status == "open"
        )
    )
    if existing:
        return ReportReceipt.model_validate(existing), False
    report = Report(video_id=video_id, reporter_id=user_id, **payload.model_dump())
    db.add(report)
    db.flush()
    result = ReportReceipt.model_validate(report)
    db.commit()
    return result, True


def video_output(video, can_watch):
    return ModerationVideo(
        id=video.id,
        owner_id=video.owner_id,
        visibility=video.visibility,
        processing_status=video.processing_status,
        moderation_status=video.moderation_status,
        moderation_reason=video.moderation_reason,
        deleted=video.deleted_at is not None,
        can_watch=can_watch,
        title=video.title if can_watch else None,
        watch_url=f"/watch/{video.id}" if can_watch else None,
    )


def video_profile(db, identifier, viewer_id):
    row = db.execute(select(Video, viewable_by(viewer_id)).where(Video.id == identifier)).first()
    if row is None:
        raise DomainError(404, "video_not_found", "Видео не найдено")
    return video_output(*row)


def report_query(viewer_id):
    return select(Report, Video, viewable_by(viewer_id)).join(Video, Video.id == Report.video_id)


def report_output(row):
    report, video, can_watch = row
    return StaffReport(
        **ReportReceipt.model_validate(report).model_dump(),
        reporter_id=report.reporter_id,
        decided_at=report.decided_at,
        decided_by=report.decided_by,
        decision_reason=report.decision_reason,
        video=video_output(video, can_watch),
    )


def report_profile(db, identifier, viewer_id):
    row = db.execute(report_query(viewer_id).where(Report.id == identifier)).first()
    if row is None:
        raise DomainError(404, "report_not_found", "Жалоба не найдена")
    return report_output(row)


def list_reports(db, viewer_id, status, limit, cursor):
    scope = f"reports:{viewer_id}:{status}"
    position = decode_cursor(cursor, scope)
    query = report_query(viewer_id)
    if status != "all":
        query = query.where(Report.status == status)
    if position:
        query = query.where(tuple_(Report.created_at, Report.id) < position)
    rows = db.execute(
        query.order_by(Report.created_at.desc(), Report.id.desc()).limit(limit + 1)
    ).all()
    page = rows[:limit]
    return ReportPage(
        items=[report_output(row) for row in page],
        next_cursor=encode_cursor(scope, page[-1][0].created_at, page[-1][0].id)
        if len(rows) > limit
        else None,
    )


def decide(db, identifier, user_id, payload):
    staff_actor(db, user_id)
    report = db.get(Report, identifier)
    if report is None:
        raise DomainError(404, "report_not_found", "Жалоба не найдена")
    locked_video(db, report.video_id)
    report = db.scalar(
        select(Report)
        .where(Report.id == identifier)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if report.status != "open":
        if report.status == payload.status and report.decision_reason == payload.reason:
            return report_profile(db, identifier, user_id)
        raise DomainError(409, "report_closed", "Жалоба уже рассмотрена. Обновите страницу.")
    report.status = payload.status
    report.decided_by = user_id
    report.decided_at = datetime.now(UTC)
    report.decision_reason = payload.reason
    record(
        db,
        user_id,
        "report." + payload.status,
        "report",
        identifier,
        payload.reason,
        video_id=str(report.video_id),
        previous_status="open",
        status=payload.status,
    )
    db.commit()
    return report_profile(db, identifier, user_id)


def moderate_video(db, identifier, user_id, payload):
    staff_actor(db, user_id)
    video = locked_video(db, identifier)
    if video.deleted_at is not None:
        raise DomainError(409, "video_deleted", "Удалённое видео нельзя восстановить модерацией")
    if video.moderation_status != payload.status:
        previous = video.moderation_status
        video.moderation_status = payload.status
        video.moderation_reason = payload.reason if payload.status == "hidden" else None
        record(
            db,
            user_id,
            "video.hidden" if payload.status == "hidden" else "video.restored",
            "video",
            identifier,
            payload.reason,
            previous_status=previous,
            status=payload.status,
        )
    db.commit()
    return video_profile(db, identifier, user_id)
