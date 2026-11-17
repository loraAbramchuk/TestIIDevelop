from dataclasses import dataclass
from datetime import timedelta
from shutil import disk_usage
from uuid import UUID, uuid4

from procrastinate.exceptions import ConnectorException
from sqlalchemy import exists, func, select, update

from app.models import UploadAdmission, UploadAttempt, Video
from app.uploads.queue import enqueue_processing
from app.uploads.storage import attempt_key, remove_attempt
from app.videos.service import VideoError, owned_video


class UploadError(VideoError):
    def __init__(self, status, code, message, retry=None):
        super().__init__(status, code, message)
        self.headers = {"Retry-After": str(retry)} if retry else None


@dataclass(frozen=True)
class Claim:
    id: UUID
    video_id: UUID
    expected_bytes: int | None


def lost_lease():
    return UploadError(
        409, "upload_lease_lost", "Попытка загрузки больше не активна. Проверьте статус видео"
    )


class UploadService:
    def __init__(self, settings, session_factory):
        self.settings = settings
        self.sessions = session_factory

    @staticmethod
    def mutex(db):
        row = db.scalar(select(UploadAdmission).where(UploadAdmission.id == 1).with_for_update())
        if row is None:
            raise UploadError(503, "upload_unavailable", "Приём файлов временно недоступен", 30)

    def admit(self, video_id: UUID, owner_id: UUID, expected_bytes: int | None) -> Claim:
        settings = self.settings
        reserve = settings.upload_reservation_bytes(expected_bytes)
        with self.sessions() as db:
            self.mutex(db)
            video = owned_video(db, video_id, owner_id, lock=True)
            if video.processing_status not in ("uploading", "failed"):
                raise UploadError(409, "upload_state_conflict", "Для этого видео файл уже принят")
            if video.current_upload_id:
                previous = db.get(UploadAttempt, video.current_upload_id)
                if previous and previous.state == "receiving":
                    raise UploadError(409, "upload_busy", "Для этого видео уже идёт загрузка")
            active = ("receiving", "cleanup")

            def count(*conditions):
                return db.scalar(select(func.count()).select_from(UploadAttempt).where(*conditions))

            if (
                count(UploadAttempt.state.in_(active)) >= settings.max_uploads_total
                or count(
                    UploadAttempt.state.in_(active),
                    UploadAttempt.owner_id == owner_id,
                )
                >= settings.max_uploads_per_owner
            ):
                raise UploadError(
                    429, "upload_slots_full", "Все слоты загрузки заняты. Попробуйте позже", 15
                )
            if (
                count(UploadAttempt.state.in_(("receiving", "processing")))
                >= settings.max_pending_video_jobs
            ):
                raise UploadError(503, "processing_queue_full", "Очередь обработки заполнена", 30)

            def reserved(*conditions):
                return db.scalar(
                    select(func.coalesce(func.sum(UploadAttempt.reserved_bytes), 0)).where(
                        *conditions
                    )
                )

            total = reserved(UploadAttempt.state != "cleaned")
            owned = reserved(UploadAttempt.state != "cleaned", UploadAttempt.owner_id == owner_id)
            if (
                owned + reserve > settings.owner_media_quota_bytes
                or total + reserve > settings.media_capacity_bytes
            ):
                raise UploadError(
                    507, "media_quota_exceeded", "Недостаточно доступного места по квоте"
                )
            try:
                free = disk_usage(settings.media_root).free
            except OSError:
                raise UploadError(
                    507, "storage_unavailable", "Хранилище недоступно для загрузки"
                ) from None
            if free < settings.media_min_free_bytes + total + reserve:
                raise UploadError(507, "storage_full", "Недостаточно свободного места для загрузки")
            now = db.scalar(select(func.clock_timestamp()))
            identifier = uuid4()
            attempt = UploadAttempt(
                id=identifier,
                video_id=video_id,
                owner_id=owner_id,
                reserved_bytes=reserve,
                expected_bytes=expected_bytes,
                lease_until=now + timedelta(seconds=settings.upload_lease_seconds),
            )
            db.add(attempt)
            db.flush()
            video.current_upload_id = identifier
            video.processing_status = "uploading"
            video.error_code = None
            video.source_key = None
            video.source_bytes = None
            db.commit()
            return Claim(identifier, video_id, expected_bytes)

    def renew(self, identifier: UUID, size: int) -> bool:
        with self.sessions() as db:
            now = db.scalar(select(func.clock_timestamp()))
            current = exists(
                select(Video.id).where(
                    Video.id == UploadAttempt.video_id,
                    Video.current_upload_id == identifier,
                    Video.deleted_at.is_(None),
                )
            )
            result = db.execute(
                update(UploadAttempt)
                .where(
                    UploadAttempt.id == identifier,
                    UploadAttempt.state == "receiving",
                    UploadAttempt.lease_until > now,
                    current,
                )
                .values(
                    lease_until=now + timedelta(seconds=self.settings.upload_lease_seconds),
                    actual_bytes=size,
                )
            )
            db.commit()
            return result.rowcount == 1

    def complete(self, claim: Claim, size: int):
        if not 1 <= size <= self.settings.max_upload_bytes or (
            claim.expected_bytes is not None and size != claim.expected_bytes
        ):
            raise UploadError(400, "upload_size_mismatch", "Некорректная длина файла")
        with self.sessions() as db:
            self.mutex(db)
            video = db.scalar(select(Video).where(Video.id == claim.video_id).with_for_update())
            attempt = db.scalar(
                select(UploadAttempt).where(UploadAttempt.id == claim.id).with_for_update()
            )
            now = db.scalar(select(func.clock_timestamp()))
            if (
                not video
                or video.deleted_at
                or video.current_upload_id != claim.id
                or not attempt
                or attempt.state != "receiving"
                or attempt.lease_until <= now
            ):
                raise lost_lease()
            try:
                job_id = enqueue_processing(db, self.settings, video.id, attempt.id)
            except ConnectorException:
                raise UploadError(
                    503,
                    "queue_unavailable",
                    "Не удалось поставить видео в обработку. Проверьте статус",
                    30,
                ) from None
            attempt.state = "processing"
            attempt.actual_bytes = size
            attempt.reserved_bytes = self.settings.upload_reservation_bytes(size)
            attempt.lease_until = None
            attempt.job_id = job_id
            video.source_key = attempt_key(claim.id)
            video.source_bytes = size
            video.processing_status = "processing"
            video.error_code = None
            db.commit()

    def fail(self, identifier: UUID, code: str):
        with self.sessions() as db:
            self.mutex(db)
            info = db.get(UploadAttempt, identifier)
            if info is None:
                return
            video = db.scalar(select(Video).where(Video.id == info.video_id).with_for_update())
            attempt = db.scalar(
                select(UploadAttempt)
                .where(UploadAttempt.id == identifier)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            # A committed queue entry must survive a lost HTTP response/cancellation.
            if attempt.state != "receiving":
                return
            self._mark_cleanup(video, attempt, code)
            db.commit()

    @staticmethod
    def _mark_cleanup(video, attempt, code):
        attempt.state = "cleanup"
        attempt.error_code = code
        attempt.lease_until = None
        if video and video.current_upload_id == attempt.id:
            video.processing_status = "failed"
            video.error_code = code
            video.source_key = None
            video.source_bytes = None

    def clean(self, identifier: UUID) -> bool:
        with self.sessions() as db:
            self.mutex(db)
            info = db.get(UploadAttempt, identifier)
            if info is None:
                return False
            video = db.scalar(select(Video).where(Video.id == info.video_id).with_for_update())
            attempt = db.scalar(
                select(UploadAttempt)
                .where(UploadAttempt.id == identifier)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            now = db.scalar(select(func.clock_timestamp()))
            if attempt.state == "receiving" and attempt.lease_until <= now:
                self._mark_cleanup(video, attempt, "upload_expired")
            if attempt.state not in ("cleanup", "cleaned"):
                return False
            db.commit()
        # Never hold quota/database locks during filesystem I/O.
        if not remove_attempt(self.settings.media_root, identifier):
            return False
        with self.sessions() as db:
            self.mutex(db)
            attempt = db.scalar(
                select(UploadAttempt).where(UploadAttempt.id == identifier).with_for_update()
            )
            if attempt and attempt.state in ("cleanup", "cleaned"):
                attempt.state = "cleaned"
                attempt.reserved_bytes = 0
                db.commit()
        return True

    def sweep(self, limit=100):
        with self.sessions() as db:
            now = db.scalar(select(func.clock_timestamp()))
            identifiers = list(
                db.scalars(
                    select(UploadAttempt.id)
                    .where(
                        (UploadAttempt.state == "cleanup")
                        | (
                            (UploadAttempt.state == "receiving")
                            & (UploadAttempt.lease_until <= now)
                        )
                    )
                    .order_by(UploadAttempt.created_at)
                    .limit(limit)
                )
            )
        cleaned = 0
        for identifier in identifiers:
            try:
                cleaned += self.clean(identifier)
            except OSError:
                # Permissions/space can be repaired; reservations remain held until removal.
                continue
        # Late processes can leave only empty directories after a cleaned lease is fenced.
        base = self.settings.media_root / "uploads"
        if base.is_dir() and not base.is_symlink():
            for directory in base.iterdir():
                try:
                    identifier = UUID(hex=directory.name)
                except ValueError:
                    continue
                with self.sessions() as db:
                    attempt = db.get(UploadAttempt, identifier)
                    eligible = attempt is not None and attempt.state == "cleaned"
                if eligible:
                    try:
                        remove_attempt(self.settings.media_root, identifier)
                    except OSError:
                        pass
        return cleaned
