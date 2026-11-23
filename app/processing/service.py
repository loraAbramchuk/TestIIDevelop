from dataclasses import dataclass
from datetime import timedelta
from shutil import disk_usage
from uuid import UUID, uuid4

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert

from app.models import ProcessingRun, ProcessingWorker, UploadAttempt, Video
from app.processing.errors import LeaseLost, ProcessingError, TransientProcessingError
from app.processing.storage import keys, remove_run
from app.uploads.service import UploadService


@dataclass(frozen=True)
class RunClaim:
    id: UUID
    upload_id: UUID
    video_id: UUID
    source_bytes: int
    number: int
    budget: int


class ProcessingService:
    def __init__(self, settings, sessions):
        self.settings, self.sessions = settings, sessions

    def begin(self, video_id: UUID, upload_id: UUID, job_id: int):
        """Caller holds the exclusive source lock until all media I/O has stopped."""
        with self.sessions() as db:
            UploadService.mutex(db)
            video = db.scalar(select(Video).where(Video.id == video_id).with_for_update())
            upload = db.scalar(
                select(UploadAttempt).where(UploadAttempt.id == upload_id).with_for_update()
            )
            if not video or not upload or upload.video_id != video_id or upload.job_id != job_id:
                return None
            if upload.state != "processing":
                return None
            if video.deleted_at or video.current_upload_id != upload_id:
                UploadService._mark_cleanup(video, upload, "processing_cancelled")
                db.commit()
                return None
            if video.processing_status != "processing":
                return None
            if upload.processing_attempts >= self.settings.processing_max_attempts:
                UploadService._mark_cleanup(video, upload, "processing_retries_exhausted")
                db.commit()
                raise ProcessingError("processing_retries_exhausted")
            # The source lock proves the previous media writer has released all its fds.
            for old in db.scalars(
                select(ProcessingRun)
                .where(ProcessingRun.upload_id == upload_id, ProcessingRun.state == "running")
                .with_for_update()
            ):
                old.state, old.lease_until = "cleanup", None
            now = db.scalar(select(func.clock_timestamp()))
            run = ProcessingRun(
                id=uuid4(),
                video_id=video_id,
                upload_id=upload_id,
                job_id=job_id,
                lease_until=now + timedelta(seconds=self.settings.processing_lease_seconds),
            )
            db.add(run)
            upload.current_run_id = run.id
            upload.processing_attempts += 1
            video.error_code = None
            db.commit()
            return RunClaim(
                run.id,
                upload_id,
                video_id,
                upload.actual_bytes,
                upload.processing_attempts,
                upload.reserved_bytes - upload.actual_bytes,
            )

    def renew(self, claim: RunClaim):
        with self.sessions() as db:
            run = db.scalar(
                select(ProcessingRun).where(ProcessingRun.id == claim.id).with_for_update()
            )
            # Read database time after the lock: waiting cannot resurrect an expired lease.
            now = db.scalar(select(func.clock_timestamp()))
            if not run or run.state != "running" or run.lease_until <= now:
                return False
            current = db.scalar(
                select(UploadAttempt.id)
                .join(Video, Video.id == UploadAttempt.video_id)
                .where(
                    UploadAttempt.id == claim.upload_id,
                    UploadAttempt.current_run_id == claim.id,
                    UploadAttempt.state == "processing",
                    Video.current_upload_id == claim.upload_id,
                    Video.deleted_at.is_(None),
                    Video.processing_status == "processing",
                )
            )
            if current is None:
                return False
            run.lease_until = now + timedelta(seconds=self.settings.processing_lease_seconds)
            db.commit()
            return True

    def finish(self, claim: RunClaim, info, playback_bytes, thumbnail_bytes):
        with self.sessions() as db:
            UploadService.mutex(db)
            video = db.scalar(select(Video).where(Video.id == claim.video_id).with_for_update())
            upload = db.scalar(
                select(UploadAttempt).where(UploadAttempt.id == claim.upload_id).with_for_update()
            )
            run = db.scalar(
                select(ProcessingRun).where(ProcessingRun.id == claim.id).with_for_update()
            )
            now = db.scalar(select(func.clock_timestamp()))
            if (
                not video
                or not upload
                or not run
                or video.deleted_at
                or video.processing_status != "processing"
                or video.current_upload_id != claim.upload_id
                or upload.current_run_id != claim.id
                or upload.state != "processing"
                or run.state != "running"
                or run.lease_until <= now
            ):
                raise LeaseLost("processing_lease_lost")
            used = upload.actual_bytes + playback_bytes + thumbnail_bytes
            if min(playback_bytes, thumbnail_bytes) <= 0 or used > upload.reserved_bytes:
                raise ProcessingError("processing_budget_exceeded")
            video.playback_key, video.thumbnail_key = keys(claim.id)
            video.playback_bytes, video.thumbnail_bytes = playback_bytes, thumbnail_bytes
            video.duration_seconds, video.width, video.height = (
                info.duration,
                info.width,
                info.height,
            )
            video.processing_status, video.error_code = "ready", None
            upload.state, upload.reserved_bytes = "stored", used
            run.state, run.lease_until = "complete", None
            db.commit()

    def failure(self, claim: RunClaim, code: str, *, permanent: bool):
        with self.sessions() as db:
            UploadService.mutex(db)
            video = db.scalar(select(Video).where(Video.id == claim.video_id).with_for_update())
            upload = db.scalar(
                select(UploadAttempt).where(UploadAttempt.id == claim.upload_id).with_for_update()
            )
            run = db.scalar(
                select(ProcessingRun).where(ProcessingRun.id == claim.id).with_for_update()
            )
            if not run or run.state == "complete":
                return
            run.state, run.lease_until, run.error_code = "cleanup", None, code
            if upload and upload.current_run_id == claim.id and upload.state == "processing":
                upload.current_run_id = None
                if permanent or claim.number >= self.settings.processing_max_attempts:
                    UploadService._mark_cleanup(video, upload, code)
                elif video and video.current_upload_id == claim.upload_id:
                    video.error_code = code
            db.commit()

    def clean_runs(self, upload_id: UUID):
        with self.sessions() as db:
            identifiers = list(
                db.scalars(
                    select(ProcessingRun.id).where(
                        ProcessingRun.upload_id == upload_id,
                        ProcessingRun.state.in_(("cleanup", "cleaned")),
                    )
                )
            )
        for identifier in identifiers:
            if not remove_run(self.settings.media_root, identifier):
                return False
            with self.sessions() as db:
                db.execute(
                    update(ProcessingRun)
                    .where(ProcessingRun.id == identifier, ProcessingRun.state == "cleanup")
                    .values(state="cleaned")
                )
                db.commit()
        return True

    def check_space(self):
        with self.sessions() as db:
            remaining = db.scalar(
                select(
                    func.coalesce(
                        func.sum(
                            func.greatest(
                                UploadAttempt.reserved_bytes - UploadAttempt.actual_bytes, 0
                            )
                        ),
                        0,
                    )
                ).where(UploadAttempt.state.in_(("receiving", "processing", "cleanup")))
            )
        if (
            disk_usage(self.settings.media_root).free
            < self.settings.media_min_free_bytes + remaining
        ):
            raise TransientProcessingError("processing_storage_full")

    def heartbeat(self, identifier: UUID):
        with self.sessions() as db:
            statement = insert(ProcessingWorker).values(
                id=identifier,
                heartbeat_at=func.clock_timestamp(),
                capacity=self.settings.processing_concurrency,
            )
            db.execute(
                statement.on_conflict_do_update(
                    index_elements=[ProcessingWorker.id],
                    set_={
                        "heartbeat_at": func.clock_timestamp(),
                        "capacity": self.settings.processing_concurrency,
                    },
                )
            )
            # Old monitor records are not an unbounded per-restart log.
            db.execute(
                ProcessingWorker.__table__.delete().where(
                    ProcessingWorker.heartbeat_at < func.clock_timestamp() - timedelta(days=1)
                )
            )
            db.commit()

    def recover(self, limit=100):
        """Reconcile the domain with the library queue; never dispatch media work here."""
        with self.sessions() as db:
            identifiers = list(
                db.scalars(
                    text("""
                        SELECT u.id FROM upload_attempts u
                        JOIN video_queue.procrastinate_jobs j ON j.id=u.job_id
                        WHERE u.state='processing' OR j.status='doing'
                           OR (u.state='stored' AND j.status='failed')
                        ORDER BY u.created_at LIMIT :limit
                    """),
                    {"limit": limit},
                )
            )
        recovered = 0
        for identifier in identifiers:
            with self.sessions() as db:
                UploadService.mutex(db)
                info = db.get(UploadAttempt, identifier)
                if not info:
                    continue
                video = db.scalar(select(Video).where(Video.id == info.video_id).with_for_update())
                upload = db.scalar(
                    select(UploadAttempt)
                    .where(UploadAttempt.id == identifier)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
                processing = upload.state == "processing"
                run = (
                    db.scalar(
                        select(ProcessingRun)
                        .where(ProcessingRun.id == upload.current_run_id)
                        .with_for_update()
                    )
                    if upload.current_run_id
                    else None
                )
                now = db.scalar(select(func.clock_timestamp()))
                expired = run and run.state == "running" and run.lease_until <= now
                if run and run.state == "running" and not expired:
                    continue
                if expired:
                    run.state, run.lease_until, run.error_code = (
                        "cleanup",
                        None,
                        "processing_interrupted",
                    )
                    upload.current_run_id = None
                if processing and (
                    video is None or video.deleted_at or video.current_upload_id != identifier
                ):
                    UploadService._mark_cleanup(video, upload, "processing_cancelled")
                elif (
                    processing
                    and expired
                    and upload.processing_attempts >= self.settings.processing_max_attempts
                ):
                    UploadService._mark_cleanup(video, upload, "processing_retries_exhausted")
                # Use the library's schema/functions, on this same locked transaction.
                # Library acknowledgements are separately fenced by worker ownership.
                row = db.execute(
                    text("""
                    SELECT j.status, j.worker_id, w.last_heartbeat
                    FROM video_queue.procrastinate_jobs j
                    LEFT JOIN video_queue.procrastinate_workers w ON w.id=j.worker_id
                    WHERE j.id=:id FOR UPDATE OF j
                """),
                    {"id": upload.job_id},
                ).first()
                stalled = (
                    row
                    and row.status == "doing"
                    and (
                        row.last_heartbeat is None
                        or row.last_heartbeat
                        <= now - timedelta(seconds=self.settings.processing_lease_seconds)
                    )
                )
                if row and (
                    (row.status in ("failed", "aborted") and processing)
                    or (row.status == "failed" and upload.state == "stored")
                    or (row.status == "doing" and (expired or stalled))
                ):
                    upload.recovery_attempts += 1
                    if (
                        upload.state == "processing"
                        and upload.recovery_attempts > self.settings.processing_max_attempts
                    ):
                        UploadService._mark_cleanup(video, upload, "processing_retries_exhausted")
                    if row.status == "aborted":
                        db.execute(
                            text(
                                "UPDATE video_queue.procrastinate_jobs SET status='failed' "
                                "WHERE id=:id AND status='aborted'"
                            ),
                            {"id": upload.job_id},
                        )
                    db.execute(
                        text(
                            "SELECT video_queue.procrastinate_retry_job_v2("
                            ":id, :at, NULL, NULL, NULL)"
                        ),
                        {
                            "id": upload.job_id,
                            "at": now + timedelta(seconds=self.settings.processing_retry_seconds),
                        },
                    )
                    recovered += 1
                db.commit()
        return recovered
