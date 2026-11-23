"""Real PostgreSQL transactions and streamed ASGI bodies (no savepoint-shared sessions)."""

import asyncio
import errno
import os
import secrets
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select, text, update

from app.application import create_app
from app.config import Settings
from app.models import AuthRateLimit, AuthSession, UploadAttempt, User, Video
from app.security import csrf_token, token_hash
from app.uploads import service as upload_service
from app.uploads.router import io_call
from app.uploads.service import UploadError, UploadService
from app.uploads.storage import UploadWriter, attempt_key

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_DB_TESTS") != "1", reason="Requires PostgreSQL"),
]


@pytest.fixture
def uploads(tmp_path):
    settings = Settings(
        media_root=tmp_path / "media",
        max_upload_bytes=1024,
        preview_budget_bytes=128,
        owner_media_quota_bytes=10000,
        media_capacity_bytes=100000,
        media_min_free_bytes=0,
    )
    app = create_app(settings)
    actors = {}
    with app.state.session_factory() as db:
        for name, role in (("owner", "user"), ("other", "user"), ("admin", "admin")):
            user = User(
                email=f"{uuid4().hex}@example.test",
                display_name=name,
                password_hash="unused-in-upload-tests",
                role=role,
            )
            db.add(user)
            db.flush()
            token = secrets.token_urlsafe(32)
            db.add(
                AuthSession(
                    token_hash=token_hash(token),
                    user_id=user.id,
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                )
            )
            actors[name] = (user.id, token)
        db.commit()
    factory = app.state.session_factory

    def video(owner="owner"):
        with factory() as db:
            row = Video(owner_id=actors[owner][0], title="Upload sample")
            db.add(row)
            db.commit()
            return row.id

    def headers(actor="owner", **extra):
        token = actors[actor][1]
        return {
            "Cookie": f"video_session={token}",
            "X-CSRF-Token": csrf_token(token),
            "Content-Type": "application/octet-stream",
            **extra,
        }

    with TestClient(app, base_url="https://testserver") as client:
        yield SimpleNamespace(
            app=app,
            settings=settings,
            factory=factory,
            client=client,
            video=video,
            actors=actors,
            headers=headers,
            service=UploadService(settings, factory),
        )
    with factory() as db:
        owners = [item[0] for item in actors.values()]
        jobs = list(
            db.scalars(
                select(UploadAttempt.job_id).where(
                    UploadAttempt.owner_id.in_(owners), UploadAttempt.job_id.is_not(None)
                )
            )
        )
        if jobs:
            db.execute(
                text("DELETE FROM video_queue.procrastinate_jobs WHERE id = ANY(:ids)"),
                {"ids": jobs},
            )
        db.execute(update(Video).where(Video.owner_id.in_(owners)).values(current_upload_id=None))
        db.execute(delete(UploadAttempt).where(UploadAttempt.owner_id.in_(owners)))
        db.execute(delete(Video).where(Video.owner_id.in_(owners)))
        db.execute(delete(User).where(User.id.in_(owners)))
        keys = [token_hash(f"videos:upload:{owner}") for owner in owners]
        db.execute(delete(AuthRateLimit).where(AuthRateLimit.key.in_(keys)))
        db.commit()
    app.state.engine.dispose()


def put(uploads, video_id, data=b"sample", **headers):
    return uploads.client.put(
        f"/api/videos/{video_id}/content", content=data, headers=uploads.headers(**headers)
    )


def attempt(uploads, video_id):
    with uploads.factory() as db:
        return db.scalar(
            select(UploadAttempt)
            .where(UploadAttempt.video_id == video_id)
            .order_by(UploadAttempt.created_at.desc())
        )


def expire(uploads, identifier):
    with uploads.factory() as db:
        db.execute(
            update(UploadAttempt)
            .where(UploadAttempt.id == identifier)
            .values(lease_until=func.clock_timestamp() - timedelta(seconds=1))
        )
        db.commit()


def test_durable_source_and_job_share_committed_transaction(uploads):
    identifier = uploads.video()
    response = put(uploads, identifier, b"media-bytes")
    assert response.status_code == 202, response.text
    assert response.json()["processing_status"] == "processing"
    row = attempt(uploads, identifier)
    assert row.state == "processing" and row.actual_bytes == 11 and row.job_id
    assert row.reserved_bytes == uploads.settings.upload_reservation_bytes(11)
    source = uploads.settings.media_root / attempt_key(row.id)
    assert source.read_bytes() == b"media-bytes"
    assert not source.with_suffix(".part").exists()
    with uploads.factory() as db:
        video = db.get(Video, identifier)
        assert video.current_upload_id == row.id and video.source_bytes == 11
        job = db.execute(
            text(
                "SELECT task_name, args, status FROM video_queue.procrastinate_jobs WHERE id = :id"
            ),
            {"id": row.job_id},
        ).one()
        assert job.task_name == "videos.process" and job.status == "todo"
        assert job.args == {"video_id": str(identifier), "attempt_id": str(row.id)}
    assert put(uploads, identifier).status_code == 409
    for path in (
        f"/media/{attempt_key(row.id)}",
        f"/{attempt_key(row.id)}",
        f"/api/videos/{identifier}",
    ):
        assert uploads.client.get(path).status_code == 404
    # A lost response after commit must not delete the accepted source/job.
    uploads.service.fail(row.id, "lost_response")
    assert not uploads.service.clean(row.id)
    assert source.read_bytes() == b"media-bytes"


@pytest.mark.parametrize(
    "actor,csrf,status",
    [(None, True, 401), ("owner", False, 403), ("other", True, 404), ("admin", True, 404)],
)
def test_auth_and_ownership_reject_before_reading_body(uploads, actor, csrf, status):
    identifier = uploads.video()
    headers = uploads.headers(actor) if actor else {"Content-Type": "application/octet-stream"}
    if not csrf:
        headers.pop("X-CSRF-Token")
    consumed = []

    async def body():
        consumed.append(True)
        yield b"secret"

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=uploads.app), base_url="https://testserver"
        ) as client:
            return await client.put(
                f"/api/videos/{identifier}/content", content=body(), headers=headers
            )

    assert asyncio.run(run()).status_code == status
    assert consumed == []
    assert attempt(uploads, identifier) is None


@pytest.mark.parametrize(
    "length,data,status",
    [
        ("1025", b"x", 413),
        ("0", b"", 400),
        ("-1", b"x", 400),
        ("abc", b"x", 400),
        ("2", b"123", 413),
        ("4", b"123", 400),
    ],
)
def test_announced_and_actual_lengths(uploads, length, data, status):
    identifier = uploads.video()
    response = put(uploads, identifier, data, **{"Content-Length": length})
    assert response.status_code == status, response.text
    row = attempt(uploads, identifier)
    if row:
        assert row.state == "cleaned" and row.reserved_bytes == 0
        assert not (uploads.settings.media_root / "uploads" / row.id.hex).exists()


@pytest.mark.parametrize(
    "chunks,status", [([b"a" * 500, b"b" * 524], 202), ([b"a" * 600, b"b" * 425], 413), ([], 400)]
)
def test_chunked_unknown_length_is_bounded(uploads, chunks, status):
    identifier = uploads.video()

    async def body():
        for chunk in chunks:
            yield chunk

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=uploads.app), base_url="https://testserver"
        ) as client:
            return await client.put(
                f"/api/videos/{identifier}/content", content=body(), headers=uploads.headers()
            )

    response = asyncio.run(run())
    assert response.status_code == status, response.text
    row = attempt(uploads, identifier)
    assert row.state == ("processing" if status == 202 else "cleaned")


def test_content_type_and_upload_rate_limit(uploads):
    identifier = uploads.video()
    assert put(uploads, identifier, **{"Content-Type": "video/mp4"}).status_code == 415
    uploads.settings.upload_limit_per_hour = 1
    response = put(uploads, identifier)
    assert response.status_code == 429 and int(response.headers["retry-after"]) > 0


def test_disk_full_removes_partial_and_releases_reservation(uploads, monkeypatch):
    identifier = uploads.video()

    def full(self, chunk):
        os.write(self.file_fd, b"partial")
        raise OSError(errno.ENOSPC, "simulated")

    monkeypatch.setattr(UploadWriter, "write", full)
    response = put(uploads, identifier)
    assert response.status_code == 507
    row = attempt(uploads, identifier)
    assert row.error_code == "storage_full" and row.state == "cleaned"
    assert row.reserved_bytes == 0
    assert not (uploads.settings.media_root / "uploads" / row.id.hex).exists()


def test_enqueue_failure_rolls_back_job_then_cleans_renamed_source(uploads, monkeypatch):
    identifier = uploads.video()
    original = upload_service.enqueue_processing
    enqueued = []

    def fail(db, settings, video_id, attempt_id):
        enqueued.append(original(db, settings, video_id, attempt_id))
        raise UploadError(503, "simulated_queue_error", "Queue failed")

    monkeypatch.setattr(upload_service, "enqueue_processing", fail)
    assert put(uploads, identifier).status_code == 503
    assert enqueued
    row = attempt(uploads, identifier)
    assert row.state == "cleaned" and row.job_id is None
    assert not (uploads.settings.media_root / attempt_key(row.id)).exists()
    with uploads.factory() as db:
        assert (
            db.scalar(
                text("SELECT count(*) FROM video_queue.procrastinate_jobs WHERE id = :id"),
                {"id": enqueued[0]},
            )
            == 0
        )
        assert db.get(Video, identifier).processing_status == "failed"


def test_same_video_concurrent_admission_has_one_writer(uploads):
    identifier = uploads.video()
    barrier = threading.Barrier(2)

    def admit(_):
        barrier.wait()
        try:
            return uploads.service.admit(identifier, uploads.actors["owner"][0], 100)
        except UploadError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(admit, range(2)))
    assert sum(isinstance(result, UploadError) for result in results) == 1
    assert (
        next(result for result in results if isinstance(result, UploadError)).code == "upload_busy"
    )


@pytest.mark.parametrize(
    "limit,code,status",
    [
        ("slots", "upload_slots_full", 429),
        ("queue", "processing_queue_full", 503),
        ("owner", "media_quota_exceeded", 507),
        ("global", "media_quota_exceeded", 507),
        ("disk", "storage_full", 507),
    ],
)
def test_atomic_admission_limits(uploads, monkeypatch, limit, code, status):
    owner = uploads.actors["owner"][0]
    if limit == "slots":
        uploads.settings.max_uploads_per_owner = 1
    elif limit == "queue":
        uploads.settings.max_pending_video_jobs = 1
    elif limit == "owner":
        uploads.settings.owner_media_quota_bytes = 3000
    elif limit == "global":
        uploads.settings.media_capacity_bytes = 3000
    elif limit == "disk":
        monkeypatch.setattr(upload_service, "disk_usage", lambda _: SimpleNamespace(free=3000))
    identifiers = [uploads.video(), uploads.video()]
    barrier = threading.Barrier(2)

    def admit(identifier):
        barrier.wait()
        try:
            return uploads.service.admit(identifier, owner, None)
        except UploadError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(admit, identifiers))
    errors = [result for result in results if isinstance(result, UploadError)]
    assert len(errors) == 1 and errors[0].code == code and errors[0].status == status


def test_live_file_lock_prevents_cleanup_and_releasing_quota(uploads):
    identifier = uploads.video()
    claim = uploads.service.admit(identifier, uploads.actors["owner"][0], 100)
    writer = UploadWriter(uploads.settings.media_root, claim.id)
    try:
        writer.start()
        writer.write(b"abc")
        expire(uploads, claim.id)
        assert not uploads.service.clean(claim.id)
        row = attempt(uploads, identifier)
        assert row.state == "cleanup" and row.reserved_bytes > 0
        assert (uploads.settings.media_root / "uploads" / claim.id.hex / "source.part").exists()
        assert not uploads.service.renew(claim.id, 3)
        with pytest.raises(UploadError, match="."):
            uploads.service.complete(claim, 100)
    finally:
        writer.close()
    assert uploads.service.sweep() == 1
    assert attempt(uploads, identifier).reserved_bytes == 0


def test_expired_writer_cannot_replace_a_new_attempt(uploads):
    identifier = uploads.video()
    owner = uploads.actors["owner"][0]
    old = uploads.service.admit(identifier, owner, 3)
    writer = UploadWriter(uploads.settings.media_root, old.id)
    writer.start()
    writer.write(b"old")
    writer.finish()
    writer.close()
    expire(uploads, old.id)
    assert uploads.service.sweep() == 1
    fresh = uploads.service.admit(identifier, owner, 3)
    with pytest.raises(UploadError) as error:
        uploads.service.complete(old, 3)
    assert error.value.code == "upload_lease_lost"
    uploads.service.fail(old.id, "late_failure")
    with uploads.factory() as db:
        row = db.get(Video, identifier)
        assert row.current_upload_id == fresh.id and row.processing_status == "uploading"


def test_heartbeat_keeps_slow_stream_alive(uploads):
    uploads.settings.upload_heartbeat_seconds = 0.1
    uploads.settings.upload_lease_seconds = 0.4
    uploads.settings.upload_idle_timeout_seconds = 1
    identifier = uploads.video()

    async def body():
        for _ in range(3):
            yield b"a"
            await asyncio.sleep(0.25)
            assert uploads.service.sweep() == 0

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=uploads.app), base_url="https://testserver"
        ) as client:
            return await client.put(
                f"/api/videos/{identifier}/content", content=body(), headers=uploads.headers()
            )

    response = asyncio.run(run())
    assert response.status_code == 202, response.text


@pytest.mark.parametrize("kind", ["idle", "total", "disconnect"])
def test_interrupted_stream_is_cleaned(uploads, kind):
    from starlette.requests import ClientDisconnect

    identifier = uploads.video()
    uploads.settings.upload_idle_timeout_seconds = 0.1 if kind == "idle" else 1
    uploads.settings.upload_timeout_seconds = 0.15 if kind == "total" else 2

    async def body():
        yield b"a"
        if kind == "disconnect":
            raise ClientDisconnect()
        for _ in range(10):
            await asyncio.sleep(0.05 if kind == "total" else 0.3)
            yield b"b"

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=uploads.app), base_url="https://testserver"
        ) as client:
            return await client.put(
                f"/api/videos/{identifier}/content", content=body(), headers=uploads.headers()
            )

    assert asyncio.run(run()).status_code == 408
    row = attempt(uploads, identifier)
    assert row.state == "cleaned" and row.reserved_bytes == 0


def test_cancellation_waits_for_disk_write_before_closing_fd(uploads):
    started = threading.Event()
    finish = threading.Event()
    closed = []

    def blocking_write():
        started.set()
        assert finish.wait(3)
        closed.append("write_done")

    async def run():
        task = asyncio.create_task(io_call(blocking_write))
        await asyncio.to_thread(started.wait, 2)
        task.cancel()
        await asyncio.sleep(0.02)
        assert not task.done()
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        closed.append("safe_to_close")

    asyncio.run(run())
    assert closed == ["write_done", "safe_to_close"]


def test_upload_page_defaults_private_and_requires_login(uploads):
    assert uploads.client.get("/upload", follow_redirects=False).status_code == 303
    response = uploads.client.get("/upload", headers=uploads.headers())
    assert response.status_code == 200 and 'private" selected' in response.text
    assert response.headers["cache-control"] == "private, no-store"
    assert 'id="upload-progress"' in response.text


def test_failed_cleanup_keeps_reservation_until_files_are_removed(uploads, monkeypatch):
    identifier = uploads.video()
    claim = uploads.service.admit(identifier, uploads.actors["owner"][0], 100)
    writer = UploadWriter(uploads.settings.media_root, claim.id)
    writer.start()
    writer.write(b"partial")
    writer.close()
    uploads.service.fail(claim.id, "interrupted")
    original = upload_service.remove_attempt

    def fail(*args):
        raise OSError(errno.EACCES, "simulated cleanup permission failure")

    monkeypatch.setattr(upload_service, "remove_attempt", fail)
    assert uploads.service.sweep() == 0
    row = attempt(uploads, identifier)
    assert row.state == "cleanup" and row.reserved_bytes > 0
    monkeypatch.setattr(upload_service, "remove_attempt", original)
    assert uploads.service.sweep() == 1
    assert attempt(uploads, identifier).reserved_bytes == 0


def test_global_slots_apply_across_different_owners(uploads):
    uploads.settings.max_uploads_total = 1
    first = uploads.video()
    second = uploads.video("other")
    uploads.service.admit(first, uploads.actors["owner"][0], 100)
    with pytest.raises(UploadError) as error:
        uploads.service.admit(second, uploads.actors["other"][0], 100)
    assert error.value.code == "upload_slots_full"


def test_cancelled_request_cleans_its_partial_file(uploads):
    identifier = uploads.video()
    started = asyncio.Event()

    async def body():
        yield b"partial"
        started.set()
        await asyncio.Event().wait()

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=uploads.app), base_url="https://testserver"
        ) as client:
            task = asyncio.create_task(
                client.put(
                    f"/api/videos/{identifier}/content", content=body(), headers=uploads.headers()
                )
            )
            await asyncio.wait_for(started.wait(), 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(run())
    row = attempt(uploads, identifier)
    assert row.state == "cleaned" and row.reserved_bytes == 0
    assert not (uploads.settings.media_root / "uploads" / row.id.hex).exists()
