"""Atomic tombstone, publication fencing and durable cleanup in upload_attempts."""

from sqlalchemy import func, select

from app.models import ProcessingRun, UploadAttempt, Video
from app.uploads.queue import cancel_processing
from app.uploads.service import UploadService
from app.videos.service import not_found


def delete_video(db, settings, video_id, owner_id):
    # Same lock order as admission, finish and maintenance; no filesystem I/O here.
    UploadService.mutex(db)
    video = db.scalar(
        select(Video).where(Video.id == video_id, Video.owner_id == owner_id).with_for_update()
    )
    if video is None:
        raise not_found()
    if video.deleted_at is not None:
        db.commit()
        return
    video.deleted_at = db.scalar(select(func.clock_timestamp()))
    attempts = list(
        db.scalars(
            select(UploadAttempt)
            .where(UploadAttempt.video_id == video_id, UploadAttempt.state != "cleaned")
            .order_by(UploadAttempt.id)
            .with_for_update()
        )
    )
    for attempt in attempts:
        UploadService._mark_cleanup(video, attempt, "video_deleted")
        attempt.current_run_id = None
    for run in db.scalars(
        select(ProcessingRun)
        .where(ProcessingRun.video_id == video_id, ProcessingRun.state != "cleaned")
        .order_by(ProcessingRun.id)
        .with_for_update()
    ):
        run.state, run.lease_until, run.error_code = "cleanup", None, "video_deleted"
    video.current_upload_id = None
    video.source_key = video.playback_key = video.thumbnail_key = None
    video.source_bytes = video.playback_bytes = video.thumbnail_bytes = None
    cancel_processing(db, settings, [attempt.job_id for attempt in attempts if attempt.job_id])
    # The cleanup states and quota reservations survive a maintenance/worker outage.
    db.commit()
