import asyncio
import errno
import logging
import time
from typing import Annotated
from uuid import UUID

from anyio import CancelScope
from fastapi import APIRouter, Depends, Request
from sqlalchemy.exc import SQLAlchemyError
from starlette.requests import ClientDisconnect

from app.auth_dependencies import Db
from app.auth_protection import rate_limit
from app.uploads.service import UploadError, UploadService, lost_lease
from app.uploads.storage import UploadWriter
from app.video_contracts import ContractError, UploadAccepted
from app.videos.router import Writer

logger = logging.getLogger(__name__)
router = APIRouter(tags=["video-upload"])


async def io_call(function, *args):
    """Don't close/unlink an fd while a canceled to_thread write is still running."""
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancelled = True
    if cancelled:
        if isinstance(result, UploadWriter):
            result.close()
        raise asyncio.CancelledError
    return result


def upload_actor(request: Request, user: Writer, db: Db) -> UUID:
    actor = user.id
    rate_limit(db, f"videos:upload:{actor}", request.app.state.settings.upload_limit_per_hour, 3600)
    # Authentication is complete: no database transaction remains open during the body stream.
    db.close()
    return actor


Actor = Annotated[UUID, Depends(upload_actor)]


@router.put(
    "/api/videos/{video_id}/content",
    response_model=UploadAccepted,
    status_code=202,
    responses={
        code: {"model": ContractError}
        for code in (400, 401, 403, 404, 408, 409, 413, 415, 429, 503, 507)
    },
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}
            },
        }
    },
    description=(
        "Потоковая передача. 202 означает файл на диске и задание в PostgreSQL, "
        "а не готовность к просмотру."
    ),
)
async def upload(video_id: UUID, request: Request, actor: Actor):
    settings = request.app.state.settings
    if (
        request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        != "application/octet-stream"
    ):
        raise UploadError(
            415, "invalid_content_type", "Передавайте файл как application/octet-stream"
        )
    length = request.headers.get("content-length")
    if length is not None:
        if not length.isascii() or not length.isdecimal():
            raise UploadError(400, "invalid_content_length", "Некорректная длина файла")
        if len(length) > 20:
            raise UploadError(413, "upload_too_large", "Файл превышает допустимый размер")
        length = int(length)
        if length > settings.max_upload_bytes:
            raise UploadError(413, "upload_too_large", "Файл превышает допустимый размер")
        if length < 1:
            raise UploadError(400, "empty_upload", "Выберите непустой файл")
    service = UploadService(settings, request.app.state.session_factory)
    claim = writer = pending = None
    size = 0
    completed = False
    failure_code = "upload_interrupted"
    try:
        claim = await io_call(service.admit, video_id, actor, length)
        writer = await io_call(UploadWriter, settings.media_root, claim.id)
        if not await io_call(service.renew, claim.id, 0):
            raise lost_lease()
        await io_call(writer.start)
        stream = request.stream().__aiter__()
        started = last_data = heartbeat = time.monotonic()
        while True:
            pending = asyncio.create_task(anext(stream))
            while True:
                now = time.monotonic()
                remaining = min(
                    settings.upload_timeout_seconds - (now - started),
                    settings.upload_idle_timeout_seconds - (now - last_data),
                )
                if remaining <= 0:
                    raise UploadError(
                        408, "upload_timeout", "Передача прервана по таймауту. Повторите загрузку"
                    )
                if now - heartbeat >= settings.upload_heartbeat_seconds:
                    if not await io_call(service.renew, claim.id, size):
                        raise lost_lease()
                    heartbeat = time.monotonic()
                if pending.done():
                    break
                await asyncio.wait(
                    {pending},
                    timeout=min(
                        remaining,
                        max(
                            0.01, settings.upload_heartbeat_seconds - (time.monotonic() - heartbeat)
                        ),
                    ),
                )
            try:
                chunk = pending.result()
            except StopAsyncIteration:
                pending = None
                break
            pending = None
            if not chunk:
                continue
            last_data = time.monotonic()
            if size + len(chunk) > settings.max_upload_bytes or (
                length is not None and size + len(chunk) > length
            ):
                raise UploadError(413, "upload_too_large", "Файл превышает допустимый размер")
            for offset in range(0, len(chunk), 1024**2):
                piece = memoryview(chunk)[offset : offset + 1024**2]
                await io_call(writer.write, piece)
                size += len(piece)
                if time.monotonic() - heartbeat >= settings.upload_heartbeat_seconds:
                    if not await io_call(service.renew, claim.id, size):
                        raise lost_lease()
                    heartbeat = time.monotonic()
        if size == 0 or (length is not None and size != length):
            raise UploadError(
                400, "upload_size_mismatch", "Длина полученного файла не совпадает с ожидаемой"
            )
        await io_call(writer.finish)
        if time.monotonic() - started >= settings.upload_timeout_seconds:
            raise UploadError(408, "upload_timeout", "Превышено время загрузки. Повторите попытку")
        await io_call(service.complete, claim, size)
        completed = True
        return UploadAccepted(id=video_id, status_url=f"/api/videos/{video_id}/status")
    except UploadError as error:
        failure_code = error.code
        raise
    except ClientDisconnect:
        failure_code = "upload_disconnected"
        raise UploadError(
            408, "upload_disconnected", "Соединение прервано. Проверьте статус и повторите загрузку"
        ) from None
    except OSError as error:
        failure_code = "storage_error"
        if error.errno in (errno.ENOSPC, errno.EDQUOT, errno.EROFS, errno.EACCES):
            failure_code = "storage_full"
            raise UploadError(
                507, "storage_full", "Хранилище недоступно для записи или закончилось место"
            ) from None
        raise UploadError(
            503, "storage_error", "Не удалось сохранить файл. Повторите попытку", 30
        ) from None
    except SQLAlchemyError:
        failure_code = "upload_database_unavailable"
        raise UploadError(
            503,
            "upload_database_unavailable",
            "Не удалось завершить загрузку. Проверьте статус",
            30,
        ) from None
    finally:
        # Starlette can cancel its entire AnyIO task group on disconnect. Shield
        # cleanup as well as disk I/O so quota release is not canceled halfway.
        with CancelScope(shield=True):
            if pending is not None and not pending.done():
                pending.cancel()
                try:
                    await pending
                except (asyncio.CancelledError, StopAsyncIteration, ClientDisconnect):
                    pass
            if writer is not None:
                await io_call(writer.close)
            if claim is not None and not completed:
                # fail() deliberately ignores a processing attempt whose commit already succeeded.
                try:
                    await io_call(service.fail, claim.id, failure_code)
                    await io_call(service.clean, claim.id)
                except (SQLAlchemyError, OSError):
                    logger.warning(
                        "Upload cleanup deferred to maintenance for attempt %s", claim.id
                    )
