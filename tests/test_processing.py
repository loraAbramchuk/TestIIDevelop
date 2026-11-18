import asyncio
import copy
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from procrastinate.jobs import Status
from sqlalchemy import delete, func, select, text, update

from app.application import create_app
from app.config import Settings
from app.models import AuthSession, ProcessingRun, ProcessingWorker, UploadAttempt, User, Video
from app.processing.errors import LeaseLost, ProcessingBusy, ProcessingError
from app.processing.media import MediaInfo, MediaProcessor, validate_probe
from app.processing.runner import process_video
from app.processing.service import ProcessingService
from app.processing.storage import RunDirectory, Slot, remove_run
from app.processing.worker import build_worker
from app.security import csrf_token, token_hash
from app.uploads.service import UploadService


@pytest.fixture(scope="module")
def samples(tmp_path_factory):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("Install FFmpeg/ffprobe to run actual media processing checks")
    root = tmp_path_factory.mktemp("media-samples")
    subprocess.run(
        [sys.executable, "experiments/make_media_samples.py", "--output", str(root)],
        check=True,
        capture_output=True,
        timeout=90,
    )
    for name, arguments in (
        (
            "one-frame",
            [
                "-f",
                "lavfi",
                "-i",
                "color=c=red:s=320x180:r=24",
                "-frames:v",
                "1",
                "-c:v",
                "libx264",
                "-threads",
                "1",
                "-profile:v",
                "main",
                "-level:v",
                "3.1",
                "-pix_fmt",
                "yuv420p",
            ],
        ),
        (
            "timestamp-offset",
            ["-i", str(root / "h264-aac.mp4"), "-c", "copy", "-output_ts_offset", "5"],
        ),
    ):
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", *arguments, str(root / f"{name}.mp4")],
            check=True,
            capture_output=True,
            timeout=30,
        )
    return root


@pytest.fixture
def processing(tmp_path):
    if os.getenv("RUN_DB_TESTS") != "1":
        pytest.skip("Requires real PostgreSQL")
    settings = Settings(
        media_root=tmp_path / "media",
        max_upload_bytes=1024**2,
        preview_budget_bytes=65536,
        owner_media_quota_bytes=10 * 1024**2,
        media_capacity_bytes=100 * 1024**2,
        media_min_free_bytes=0,
        processing_timeout_seconds=30,
        processing_lease_seconds=6,
        processing_heartbeat_seconds=1,
        processing_retry_seconds=1,
    )
    app = create_app(settings)
    factory = app.state.session_factory
    token = secrets.token_urlsafe(32)
    with factory() as db:
        user = User(
            email=f"{uuid4().hex}@example.com", display_name="Processing", password_hash="unused"
        )
        db.add(user)
        db.flush()
        owner = user.id
        db.add(
            AuthSession(
                token_hash=token_hash(token),
                user_id=owner,
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )
        db.commit()
    headers = {"Cookie": f"video_session={token}", "X-CSRF-Token": csrf_token(token)}
    with TestClient(app) as client:

        def upload(data):
            response = client.post(
                "/api/videos", json={"title": "Processing fixture"}, headers=headers
            )
            assert response.status_code == 201, response.text
            identifier = UUID(response.json()["id"])
            response = client.put(
                f"/api/videos/{identifier}/content",
                content=data,
                headers=headers | {"Content-Type": "application/octet-stream"},
            )
            assert response.status_code == 202, response.text
            with factory() as db:
                attempt = db.scalar(
                    select(UploadAttempt).where(UploadAttempt.video_id == identifier)
                )
                return identifier, attempt.id, attempt.job_id

        yield SimpleNamespace(
            settings=settings,
            factory=factory,
            app=app,
            client=client,
            owner=owner,
            headers=headers,
            upload=upload,
            service=ProcessingService(settings, factory),
        )
    with factory() as db:
        videos = list(db.scalars(select(Video.id).where(Video.owner_id == owner)))
        jobs = list(
            db.scalars(
                select(UploadAttempt.job_id).where(
                    UploadAttempt.owner_id == owner, UploadAttempt.job_id.is_not(None)
                )
            )
        )
        if jobs:
            db.execute(
                text("DELETE FROM video_queue.procrastinate_jobs WHERE id=ANY(:ids)"), {"ids": jobs}
            )
        db.execute(delete(ProcessingRun).where(ProcessingRun.video_id.in_(videos)))
        db.execute(update(Video).where(Video.owner_id == owner).values(current_upload_id=None))
        db.execute(delete(UploadAttempt).where(UploadAttempt.owner_id == owner))
        db.execute(delete(Video).where(Video.owner_id == owner))
        db.execute(delete(User).where(User.id == owner))
        db.commit()
    app.state.engine.dispose()


def execute(processing, identifiers):
    video, upload, job = identifiers
    process_video(processing.settings, processing.factory, job, str(video), str(upload))


@pytest.mark.integration
@pytest.mark.parametrize("sample", ["h264-aac", "h264-silent", "one-frame", "timestamp-offset"])
def test_actual_mp4_to_ready_preserves_source_and_produces_faststart_preview(
    processing, samples, sample
):
    original = (samples / f"{sample}.mp4").read_bytes()
    identifiers = processing.upload(original)
    execute(processing, identifiers)
    with processing.factory() as db:
        video = db.get(Video, identifiers[0])
        upload = db.get(UploadAttempt, identifiers[1])
        assert video.processing_status == "ready" and upload.state == "stored"
        assert video.visibility == "private" and video.moderation_status == "visible"
        assert video.width == 320 and video.height == 180
        if sample == "one-frame":
            assert 0 < video.duration_seconds < 0.05
        else:
            assert 1.9 < video.duration_seconds < 2.2
        root = processing.settings.media_root
        assert (root / video.source_key).read_bytes() == original
        playback = (root / video.playback_key).read_bytes()
        assert playback.index(b"moov") < playback.index(b"mdat")
        assert (root / video.thumbnail_key).read_bytes().startswith(b"\xff\xd8")
        assert upload.reserved_bytes == len(original) + len(playback) + video.thumbnail_bytes
        run = db.get(ProcessingRun, upload.current_run_id)
        assert run.state == "complete"
        keys = video.playback_key, video.thumbnail_key
        for key in keys:
            assert processing.client.get("/" + key).status_code == 404
        assert (
            processing.client.get(
                f"/api/videos/{video.id}/stream", headers=processing.headers
            ).status_code
            == 404
        )
        source_hash = hashlib.sha256(playback).digest()
    # The same library job can be delivered again; it cannot publish a second result.
    execute(processing, identifiers)
    with processing.factory() as db:
        video = db.get(Video, identifiers[0])
        assert (video.playback_key, video.thumbnail_key) == keys
        assert (
            hashlib.sha256(
                (processing.settings.media_root / video.playback_key).read_bytes()
            ).digest()
            == source_hash
        )


@pytest.mark.integration
@pytest.mark.parametrize("kind", ["garbage", "unsupported-10bit"])
def test_invalid_media_fails_safely_then_cleanup_releases_all_bytes(processing, samples, kind):
    identifiers = processing.upload(
        b"not a video" if kind == "garbage" else (samples / f"{kind}.mp4").read_bytes()
    )
    with pytest.raises(ProcessingError):
        execute(processing, identifiers)
    with processing.factory() as db:
        video = db.get(Video, identifiers[0])
        attempt = db.get(UploadAttempt, identifiers[1])
        assert video.processing_status == "failed" and video.error_code in (
            "invalid_media",
            "unsupported_media",
        )
        assert video.source_key is None and video.playback_key is None
        assert attempt.state == "cleanup" and attempt.reserved_bytes > 0
    assert UploadService(processing.settings, processing.factory).clean(identifiers[1])
    with processing.factory() as db:
        assert db.get(UploadAttempt, identifiers[1]).reserved_bytes == 0
        assert set(
            db.scalars(select(ProcessingRun.state).where(ProcessingRun.upload_id == identifiers[1]))
        ) == {"cleaned"}


@pytest.fixture
def probe_document(samples):
    return json.loads(
        subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_format",
                "-show_streams",
                "-of",
                "json",
                str(samples / "h264-aac.mp4"),
            ]
        )
    )


@pytest.mark.parametrize(
    "change",
    [
        "extra_track",
        "codec",
        "pixel_format",
        "profile",
        "level",
        "interlaced",
        "attached_picture",
        "audio_codec",
        "audio_channels",
        "audio_rate",
        "container",
        "nan_duration",
        "infinite_duration",
        "fps",
        "dimensions",
        "duration",
    ],
)
def test_probe_policy_rejects_unsupported_and_unbounded_media(probe_document, change):
    document = copy.deepcopy(probe_document)
    video = document["streams"][0]
    audio = document["streams"][1]
    if change == "extra_track":
        document["streams"].append({"codec_type": "subtitle"})
    elif change == "codec":
        video["codec_name"] = "hevc"
    elif change == "pixel_format":
        video["pix_fmt"] = "yuv420p10le"
    elif change == "profile":
        video["profile"] = "High 10"
    elif change == "level":
        video["level"] = 51
    elif change == "interlaced":
        video["field_order"] = "tt"
    elif change == "attached_picture":
        video["disposition"]["attached_pic"] = 1
    elif change == "audio_codec":
        audio["codec_name"] = "mp3"
    elif change == "audio_channels":
        audio["channels"] = 6
    elif change == "audio_rate":
        audio["sample_rate"] = "96000"
    elif change == "container":
        document["format"]["tags"]["major_brand"] = "qt  "
    elif change == "nan_duration":
        document["format"]["duration"] = "NaN"
    elif change == "infinite_duration":
        document["format"]["duration"] = "Infinity"
    elif change == "fps":
        video["avg_frame_rate"] = "120/1"
    elif change == "dimensions":
        video["width"] = 3840
    elif change == "duration":
        document["format"]["duration"] = "7201"
        video["duration"] = "7201"
    settings = Settings(_env_file=None, postgres_password="processing-policy-test-password")
    with pytest.raises(ProcessingError):
        validate_probe(document, settings)


@pytest.mark.parametrize("container_duration", ["2.022", "7.000"])
def test_probe_duration_uses_track_span_across_ffprobe_versions(probe_document, container_duration):
    document = copy.deepcopy(probe_document)
    document["format"].update(start_time="4.978", duration=container_duration)
    document["streams"][0].update(start_time="5.000", duration="2.000")
    document["streams"][1].update(start_time="4.978", duration="2.021333")
    settings = Settings(
        _env_file=None,
        postgres_password="processing-policy-test-password",
        max_video_duration_seconds=3,
    )
    assert validate_probe(document, settings).duration == pytest.approx(2.022)


@pytest.mark.parametrize("case", ["gap", "nan_start", "infinite_start", "missing_timing"])
def test_probe_rejects_unbounded_or_invalid_track_timeline(probe_document, case):
    document = copy.deepcopy(probe_document)
    if case == "gap":
        document["streams"][1]["start_time"] = "7201"
    elif case == "missing_timing":
        document["streams"][0].pop("duration")
        document["format"]["duration"] = "7201"
    else:
        document["streams"][0]["start_time"] = "NaN" if case == "nan_start" else "Infinity"
    settings = Settings(_env_file=None, postgres_password="processing-policy-test-password")
    with pytest.raises(ProcessingError):
        validate_probe(document, settings)


@pytest.mark.integration
def test_missing_source_is_reported_as_source_error(processing, samples):
    identifiers = processing.upload((samples / "h264-silent.mp4").read_bytes())
    with processing.factory() as db:
        source = db.get(Video, identifiers[0]).source_key
    (processing.settings.media_root / source).unlink()
    with pytest.raises(ProcessingError, match="source_missing_or_changed"):
        execute(processing, identifiers)
    with processing.factory() as db:
        assert db.get(Video, identifiers[0]).error_code == "source_missing_or_changed"


@pytest.mark.integration
def test_no_thumbnail_has_its_own_error_instead_of_missing_source(processing, samples, monkeypatch):
    identifiers = processing.upload((samples / "h264-silent.mp4").read_bytes())
    run = MediaProcessor.run

    def no_thumbnail(self, tool, arguments, **kwargs):
        if arguments[-1].endswith("thumbnail.part"):
            return b""
        return run(self, tool, arguments, **kwargs)

    monkeypatch.setattr(MediaProcessor, "run", no_thumbnail)
    with pytest.raises(ProcessingError, match="thumbnail_generation_failed"):
        execute(processing, identifiers)
    with processing.factory() as db:
        video = db.get(Video, identifiers[0])
        assert video.processing_status == "failed"
        assert video.error_code == "thumbnail_generation_failed"


def test_subprocess_timeout_and_lost_lease_do_not_leave_media_running(tmp_path):
    settings = Settings(_env_file=None, postgres_password="processing-policy-test-password")
    media = MediaProcessor(settings, (), lambda: True)
    with pytest.raises(ProcessingError, match="media_probe_timeout"):
        media.run(sys.executable, ["-c", "import time;time.sleep(30)"], maximum=100, timeout=0.2)
    media = MediaProcessor(settings, (), lambda: False)
    with pytest.raises(LeaseLost):
        media.run(sys.executable, ["-c", "pass"], maximum=1)


def test_file_size_cap_and_filesystem_locks(tmp_path):
    settings = Settings(_env_file=None, postgres_password="processing-policy-test-password")
    media = MediaProcessor(settings, (), lambda: True)
    target = tmp_path / "limited"
    with pytest.raises(ProcessingError):
        media.run(
            sys.executable,
            ["-c", f"with open({str(target)!r},'wb') as f: f.write(b'x'*4096)"],
            maximum=128,
        )
    assert target.stat().st_size <= 128
    slot = Slot(tmp_path, 1)
    try:
        with pytest.raises(ProcessingBusy):
            Slot(tmp_path, 1)
    finally:
        slot.close()
    run = RunDirectory(tmp_path, uuid4())
    try:
        assert not remove_run(tmp_path, run.identifier)
    finally:
        run.close()
    assert remove_run(tmp_path, run.identifier)


@pytest.mark.parametrize(
    "message,code",
    [
        ("No space left on device", "processing_storage_full"),
        ("Permission denied", "processing_storage_unavailable"),
    ],
)
def test_media_tool_storage_errors_are_transient(message, code):
    from app.processing.errors import TransientProcessingError

    settings = Settings(_env_file=None, postgres_password="processing-policy-test-password")
    media = MediaProcessor(settings, (), lambda: True)
    with pytest.raises(TransientProcessingError, match=code):
        media.run(sys.executable, ["-c", f"import sys; print({message!r}); sys.exit(1)"], maximum=1)


@pytest.mark.integration
def test_deleted_during_processing_cannot_publish_and_is_cleaned(processing, samples, monkeypatch):
    identifiers = processing.upload((samples / "h264-aac.mp4").read_bytes())
    original = MediaProcessor.prepare

    def deleted(self, *args):
        result = original(self, *args)
        with processing.factory() as db:
            db.execute(
                update(Video)
                .where(Video.id == identifiers[0])
                .values(deleted_at=func.clock_timestamp())
            )
            db.commit()
        return result

    monkeypatch.setattr(MediaProcessor, "prepare", deleted)
    with pytest.raises(ProcessingBusy):
        execute(processing, identifiers)
    processing.service.recover()
    assert UploadService(processing.settings, processing.factory).clean(identifiers[1])
    with processing.factory() as db:
        video = db.get(Video, identifiers[0])
        assert (
            video.deleted_at and video.processing_status != "ready" and video.playback_key is None
        )


@pytest.mark.integration
def test_old_generation_cannot_publish_or_fail_new_generation(processing, samples):
    video, upload, job = processing.upload((samples / "h264-aac.mp4").read_bytes())
    old = processing.service.begin(video, upload, job)
    fresh = processing.service.begin(video, upload, job)
    with pytest.raises(LeaseLost):
        processing.service.finish(old, MediaInfo(2, 320, 180, True), 100, 100)
    processing.service.failure(old, "stale_failure", permanent=True)
    with processing.factory() as db:
        attempt = db.get(UploadAttempt, upload)
        row = db.get(Video, video)
        assert attempt.current_run_id == fresh.id and attempt.state == "processing"
        assert row.processing_status == "processing" and row.error_code is None


@pytest.mark.integration
def test_real_library_worker_processes_the_committed_job(processing, samples):
    identifiers = processing.upload((samples / "h264-aac.mp4").read_bytes())

    async def work():
        queue = build_worker(processing.settings, processing.factory)
        async with queue.open_async():
            await queue.run_worker_async(
                queues=["video-processing"],
                concurrency=1,
                wait=False,
                listen_notify=False,
                install_signal_handlers=False,
            )

    asyncio.run(work())
    with processing.factory() as db:
        assert db.get(Video, identifiers[0]).processing_status == "ready"
        assert (
            db.scalar(
                text("SELECT status FROM video_queue.procrastinate_jobs WHERE id=:id"),
                {"id": identifiers[2]},
            )
            == "succeeded"
        )


@pytest.mark.integration
@pytest.mark.parametrize("same_worker", [False, True])
def test_recovery_requeues_stalled_job_and_fences_old_library_ack(processing, samples, same_worker):
    video, upload, job_id = processing.upload((samples / "h264-aac.mp4").read_bytes())

    async def work():
        queue = build_worker(processing.settings, processing.factory)
        async with queue.open_async():
            old_worker = await queue.job_manager.register_worker()
            job = await queue.job_manager.fetch_job(
                queues=["video-processing"], worker_id=old_worker
            )
            claim = processing.service.begin(video, upload, job_id)
            with processing.factory() as db:
                db.execute(
                    update(ProcessingRun)
                    .where(ProcessingRun.id == claim.id)
                    .values(lease_until=func.clock_timestamp() - timedelta(seconds=1))
                )
                db.commit()
            assert processing.service.recover() == 1
            await queue.job_manager.finish_job(job, Status.SUCCEEDED, False)
            with processing.factory() as db:
                assert (
                    db.scalar(
                        text("SELECT status FROM video_queue.procrastinate_jobs WHERE id=:id"),
                        {"id": job_id},
                    )
                    == "todo"
                )
                db.execute(
                    text(
                        "UPDATE video_queue.procrastinate_jobs SET scheduled_at=now() WHERE id=:id"
                    ),
                    {"id": job_id},
                )
                db.commit()
            new_worker = old_worker if same_worker else await queue.job_manager.register_worker()
            fresh = await queue.job_manager.fetch_job(
                queues=["video-processing"], worker_id=new_worker
            )
            await queue.job_manager.retry_job(job)
            await queue.job_manager.finish_job(job, Status.FAILED, False)
            with processing.factory() as db:
                row = db.execute(
                    text(
                        "SELECT status,worker_id FROM video_queue.procrastinate_jobs WHERE id=:id"
                    ),
                    {"id": job_id},
                ).one()
                assert row.status == "doing" and row.worker_id == new_worker
            await queue.job_manager.finish_job(fresh, Status.SUCCEEDED, False)
            await queue.job_manager.unregister_worker(old_worker)
            await queue.job_manager.unregister_worker(new_worker)

    asyncio.run(work())


@pytest.mark.integration
def test_storage_retries_are_bounded_and_keep_reservation_until_cleanup(
    processing, samples, monkeypatch
):
    import errno

    identifiers = processing.upload((samples / "h264-aac.mp4").read_bytes())

    def full(*args):
        raise OSError(errno.ENOSPC, "simulated")

    monkeypatch.setattr(MediaProcessor, "prepare", full)
    for _ in range(processing.settings.processing_max_attempts):
        with pytest.raises(ProcessingError):
            execute(processing, identifiers)
    execute(processing, identifiers)  # Redelivery after terminal failure does no more media work.
    with processing.factory() as db:
        row = db.get(Video, identifiers[0])
        attempt = db.get(UploadAttempt, identifiers[1])
        assert row.processing_status == "failed" and row.error_code == "processing_storage_full"
        assert attempt.processing_attempts == 3 and attempt.reserved_bytes > 0
    assert UploadService(processing.settings, processing.factory).clean(identifiers[1])


@pytest.mark.integration
def test_expired_domain_lease_alone_prevents_publication(processing, samples):
    video, upload, job = processing.upload((samples / "h264-aac.mp4").read_bytes())
    claim = processing.service.begin(video, upload, job)
    with processing.factory() as db:
        db.execute(
            update(ProcessingRun)
            .where(ProcessingRun.id == claim.id)
            .values(lease_until=func.clock_timestamp() - timedelta(seconds=1))
        )
        db.commit()
    assert not processing.service.renew(claim)
    with pytest.raises(LeaseLost):
        processing.service.finish(claim, MediaInfo(2, 320, 180, True), 100, 100)


@pytest.mark.integration
def test_processing_checks_physical_reserve_again_after_upload(processing, samples):
    identifiers = processing.upload((samples / "h264-aac.mp4").read_bytes())
    processing.settings.media_min_free_bytes = 2**63
    with pytest.raises(ProcessingError, match="processing_storage_full"):
        execute(processing, identifiers)
    with processing.factory() as db:
        video = db.get(Video, identifiers[0])
        assert video.processing_status == "processing"
        assert video.playback_key is None
    processing.settings.media_min_free_bytes = 0
    execute(processing, identifiers)
    with processing.factory() as db:
        assert db.get(Video, identifiers[0]).processing_status == "ready"


@pytest.mark.integration
def test_worker_heartbeat_is_diagnostic_and_does_not_gate_uploads(processing):
    response = processing.client.get("/health/diagnostics").json()
    assert response["checks"]["processing"] == "error"
    assert response["video_uploads"] == "available"
    assert processing.client.get("/health/ready").status_code == 200
    identifier = uuid4()
    try:
        processing.service.heartbeat(identifier)
        assert processing.client.get("/health/diagnostics").json()["checks"]["processing"] == "ok"
        with processing.factory() as db:
            db.execute(
                update(ProcessingWorker)
                .where(ProcessingWorker.id == identifier)
                .values(heartbeat_at=func.clock_timestamp() - timedelta(seconds=60))
            )
            db.commit()

        assert (
            processing.client.get("/health/diagnostics").json()["checks"]["processing"] == "error"
        )
    finally:
        with processing.factory() as db:
            db.execute(delete(ProcessingWorker).where(ProcessingWorker.id == identifier))
            db.commit()


@pytest.mark.integration
def test_recovery_after_ready_commit_replays_only_acknowledgement(processing, samples):
    identifiers = processing.upload((samples / "h264-aac.mp4").read_bytes())

    async def work():
        queue = build_worker(processing.settings, processing.factory)
        async with queue.open_async():
            old_worker = await queue.job_manager.register_worker()
            job = await queue.job_manager.fetch_job(
                queues=["video-processing"], worker_id=old_worker
            )
            execute(processing, identifiers)
            with processing.factory() as db:
                video = db.get(Video, identifiers[0])
                original_key = video.playback_key
                db.execute(
                    text(
                        "UPDATE video_queue.procrastinate_workers "
                        "SET last_heartbeat=now()-interval '1 hour' WHERE id=:id"
                    ),
                    {"id": old_worker},
                )
                db.commit()
            assert processing.service.recover() == 1
            await queue.job_manager.finish_job(job, Status.FAILED, False)
            with processing.factory() as db:
                assert db.get(Video, identifiers[0]).processing_status == "ready"
                db.execute(
                    text(
                        "UPDATE video_queue.procrastinate_jobs SET scheduled_at=now() WHERE id=:id"
                    ),
                    {"id": identifiers[2]},
                )
                db.commit()
            await queue.run_worker_async(
                queues=["video-processing"],
                concurrency=1,
                wait=False,
                listen_notify=False,
                install_signal_handlers=False,
            )
            with processing.factory() as db:
                assert db.get(Video, identifiers[0]).playback_key == original_key
                assert db.get(UploadAttempt, identifiers[1]).processing_attempts == 1
                assert (
                    db.scalar(
                        text("SELECT status FROM video_queue.procrastinate_jobs WHERE id=:id"),
                        {"id": identifiers[2]},
                    )
                    == "succeeded"
                )

    asyncio.run(work())


@pytest.mark.integration
def test_library_retries_a_transient_media_failure_then_succeeds(processing, samples, monkeypatch):
    import errno

    identifiers = processing.upload((samples / "h264-aac.mp4").read_bytes())
    prepare = MediaProcessor.prepare
    calls = []

    def fail_once(self, *args):
        calls.append(True)
        if len(calls) == 1:
            raise OSError(errno.ENOSPC, "simulated")
        return prepare(self, *args)

    monkeypatch.setattr(MediaProcessor, "prepare", fail_once)

    async def work():
        queue = build_worker(processing.settings, processing.factory)
        async with queue.open_async():
            options = dict(
                queues=["video-processing"],
                concurrency=1,
                wait=False,
                listen_notify=False,
                install_signal_handlers=False,
            )
            await queue.run_worker_async(**options)
            with processing.factory() as db:
                assert db.get(Video, identifiers[0]).processing_status == "processing"
                assert (
                    db.scalar(
                        text("SELECT status FROM video_queue.procrastinate_jobs WHERE id=:id"),
                        {"id": identifiers[2]},
                    )
                    == "todo"
                )
                db.execute(
                    text(
                        "UPDATE video_queue.procrastinate_jobs SET scheduled_at=now() WHERE id=:id"
                    ),
                    {"id": identifiers[2]},
                )
                db.commit()
            await queue.run_worker_async(**options)

    asyncio.run(work())
    with processing.factory() as db:
        video = db.get(Video, identifiers[0])
        assert video.processing_status == "ready" and video.error_code is None
        assert db.get(UploadAttempt, identifiers[1]).processing_attempts == 2
        assert (
            db.scalar(
                text("SELECT status FROM video_queue.procrastinate_jobs WHERE id=:id"),
                {"id": identifiers[2]},
            )
            == "succeeded"
        )
