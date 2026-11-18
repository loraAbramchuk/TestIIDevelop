import errno
import os
import stat
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError

from app.processing.errors import (
    LeaseLost,
    ProcessingBusy,
    ProcessingError,
    TransientProcessingError,
)
from app.processing.media import MediaProcessor
from app.processing.service import ProcessingService
from app.processing.storage import RunDirectory, Slot
from app.uploads.storage import UploadWriter


def process_video(
    settings, sessions, job_id: int, video_id: str, attempt_id: str, should_abort=lambda: False
):
    service = ProcessingService(settings, sessions)
    video_id, upload_id = UUID(video_id), UUID(attempt_id)
    slot = source = output = claim = None
    source_fd = None
    try:
        slot = Slot(settings.media_root, settings.processing_concurrency)
        try:
            source = UploadWriter(settings.media_root, upload_id)
        except BlockingIOError:
            raise ProcessingBusy("source_writer_busy") from None
        claim = service.begin(video_id, upload_id, job_id)
        if claim is None:
            return
        if not service.clean_runs(upload_id):
            raise ProcessingBusy("previous_output_busy")
        service.check_space()
        source_fd = os.open("source.mp4", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=source.directory_fd)
        info = os.fstat(source_fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size != claim.source_bytes:
            raise ProcessingError("source_missing_or_changed")
        output = RunDirectory(settings.media_root, claim.id)
        descriptors = (
            source_fd,
            source.directory_fd,
            source.lock_fd,
            slot.fd,
            output.fd,
            output.lock_fd,
        )
        processor = MediaProcessor(
            settings, descriptors, lambda: not should_abort() and service.renew(claim)
        )
        result = processor.prepare(
            f"/proc/self/fd/{source_fd}", output, claim.source_bytes, claim.budget
        )
        service.finish(claim, result, output.size("playback.mp4"), output.size("thumbnail.jpg"))
    except LeaseLost:
        if claim:
            service.failure(claim, "processing_interrupted", permanent=False)
        raise ProcessingBusy("processing_interrupted") from None
    except ProcessingError as error:
        if claim:
            service.failure(
                claim, error.code, permanent=not isinstance(error, TransientProcessingError)
            )
        raise
    except FileNotFoundError:
        if claim:
            service.failure(claim, "source_missing_or_changed", permanent=True)
        raise ProcessingError("source_missing_or_changed") from None
    except OSError as error:
        code = (
            "processing_storage_full"
            if error.errno in (errno.ENOSPC, errno.EDQUOT)
            else "processing_storage_unavailable"
        )
        if claim:
            service.failure(claim, code, permanent=False)
            if claim.number >= settings.processing_max_attempts:
                raise ProcessingError(code) from None
        raise TransientProcessingError(code) from None
    except SQLAlchemyError:
        # Leave the durable lease and files for recovery if the DB itself is unavailable.
        raise TransientProcessingError("processing_database_unavailable") from None
    finally:
        if source_fd is not None:
            os.close(source_fd)
        if output:
            output.close()
        if source:
            source.close()
        if slot:
            slot.close()
