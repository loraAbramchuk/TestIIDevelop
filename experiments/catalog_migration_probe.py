"""Verify 0006 -> 0007 -> 0006 -> 0007 only in an empty *_probe database."""

import hashlib
import json
import secrets
import subprocess
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import inspect, select, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.application import create_app  # noqa: E402
from app.config import Settings  # noqa: E402
from app.db import make_engine, make_session_factory  # noqa: E402
from app.models import AuthSession, Channel, ProcessingRun, UploadAttempt, User, Video, VideoGrant  # noqa: E402
from app.security import token_hash  # noqa: E402
from app.uploads.queue import enqueue_processing  # noqa: E402


def migrate(*arguments):
    subprocess.run(
        [sys.executable, "-m", "alembic", *arguments], check=True, capture_output=True, timeout=60
    )


def main():
    settings = Settings()
    assert settings.postgres_db.endswith("_probe"), "Use a dedicated empty *_probe database"
    engine = make_engine(settings)
    assert not inspect(engine).has_table("users"), "Database must be empty"
    migrate("upgrade", "0006")
    factory = make_session_factory(engine)
    root = Path(tempfile.mkdtemp(prefix="stage6-migration-"))
    settings = settings.model_copy(update={"media_root": root})
    source = Path("/tmp/stage1-media/h264-aac.mp4").read_bytes()
    upload_id, run_id = uuid4(), uuid4()
    source_key = f"uploads/{upload_id.hex}/source.mp4"
    playback_key, thumbnail_key = (
        f"processing/{run_id.hex}/playback.mp4",
        f"processing/{run_id.hex}/thumbnail.jpg",
    )
    for key in (source_key, playback_key):
        path = root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(source)
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-i",
            str(root / source_key),
            "-frames:v",
            "1",
            "-threads",
            "1",
            "-pix_fmt",
            "yuvj420p",
            str(root / thumbnail_key),
        ],
        check=True,
        timeout=30,
    )
    tokens = [secrets.token_urlsafe(32) for _ in range(2)]
    with factory() as db:
        owner = User(
            email="legacy-owner@example.com",
            display_name="Прежний автор",
            password_hash="legacy-unusable-hash",
            role="user",
        )
        recipient = User(
            email="legacy-recipient@example.com",
            display_name="Прежний зритель",
            password_hash="legacy-unusable-hash",
            role="moderator",
        )
        db.add_all([owner, recipient])
        db.flush()
        owner_id, recipient_id = owner.id, recipient.id
        video = Video(
            owner_id=owner_id,
            title="Прежнее готовое видео",
            visibility="private",
            processing_status="ready",
            duration_seconds=2,
            width=320,
            height=180,
            source_key=source_key,
            source_bytes=len(source),
            playback_key=playback_key,
            playback_bytes=len(source),
            thumbnail_key=thumbnail_key,
            thumbnail_bytes=(root / thumbnail_key).stat().st_size,
        )
        db.add(video)
        db.flush()
        video_id = video.id
        upload = UploadAttempt(
            id=upload_id,
            video_id=video_id,
            owner_id=owner_id,
            state="stored",
            actual_bytes=len(source),
            expected_bytes=len(source),
            reserved_bytes=len(source) * 2 + video.thumbnail_bytes,
        )
        db.add(upload)
        db.flush()
        job = enqueue_processing(db, settings, video_id, upload_id)
        upload.job_id = job
        upload.current_run_id = run_id
        video.current_upload_id = upload_id
        db.add(
            ProcessingRun(
                id=run_id, video_id=video_id, upload_id=upload_id, job_id=job, state="complete"
            )
        )
        db.add(VideoGrant(video_id=video_id, user_id=recipient_id, granted_by=owner_id))
        for user, token in zip((owner, recipient), tokens, strict=True):
            db.add(
                AuthSession(
                    token_hash=token_hash(token),
                    user_id=user.id,
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                )
            )
        db.commit()

    def snapshot():
        with factory() as db:
            video = db.get(Video, video_id)
            upload = db.get(UploadAttempt, upload_id)
            return {
                "users": [
                    (str(u.id), u.email, u.display_name, u.password_hash, u.role)
                    for u in db.scalars(select(User).order_by(User.id))
                ],
                "sessions": [
                    (s.token_hash, str(s.user_id))
                    for s in db.scalars(select(AuthSession).order_by(AuthSession.token_hash))
                ],
                "video": [
                    str(video.id),
                    str(video.owner_id),
                    video.title,
                    video.visibility,
                    video.processing_status,
                    video.source_key,
                    video.playback_key,
                    video.thumbnail_key,
                    video.duration_seconds,
                ],
                "upload": [
                    upload.state,
                    upload.reserved_bytes,
                    upload.job_id,
                    str(upload.current_run_id),
                ],
                "grant": str(db.get(VideoGrant, (video_id, recipient_id)).granted_by),
                "queue": db.execute(
                    text(
                        "SELECT id,status::text,args::text FROM video_queue.procrastinate_jobs "
                        "WHERE id=:id"
                    ),
                    {"id": job},
                )
                .one()
                ._asdict(),
                "files": [
                    hashlib.sha256((root / key).read_bytes()).hexdigest()
                    for key in (source_key, playback_key, thumbnail_key)
                ],
            }

    before = snapshot()
    migrate("upgrade", "head")
    assert snapshot() == before
    with factory() as db:
        channels = list(db.scalars(select(Channel)))
        assert len(channels) == 2
        original_ids = {str(c.owner_id): str(c.id) for c in channels}
        assert {c.name for c in channels} == {"Прежний автор", "Прежний зритель"}
    migrate("upgrade", "head")
    with factory() as db:
        assert {str(c.owner_id): str(c.id) for c in db.scalars(select(Channel))} == original_ids
    with TestClient(create_app(settings)) as client:
        owner_headers = {"Cookie": f"video_session={tokens[0]}"}
        recipient_headers = {"Cookie": f"video_session={tokens[1]}"}
        me = client.get("/api/channels/me", headers=owner_headers)
        assert me.status_code == 200
        channel_id = me.json()["id"]
        assert client.get("/api/videos").json()["items"] == []
        assert (
            len(
                client.get(f"/api/channels/{channel_id}/videos", headers=recipient_headers).json()[
                    "items"
                ]
            )
            == 1
        )
        assert (
            client.get(f"/api/videos/{video_id}/stream", headers=recipient_headers).status_code
            == 200
        )
        assert client.get(f"/api/videos/{video_id}/stream").status_code == 404
    migrate("check")
    migrate("downgrade", "0006")
    assert snapshot() == before
    migrate("upgrade", "head")
    assert snapshot() == before
    report = {
        "result": "passed",
        "checks": [
            "legacy users get exactly one channel",
            "repeated upgrade preserves channel IDs",
            "users/roles/sessions/video/grant/queue/files preserved",
            "legacy session sees channel and permitted video",
            "anonymous discovery does not expose legacy private video",
            "downgrade/re-upgrade preserves stage 5 data",
            "alembic check clean",
        ],
    }
    Path("/tmp/stage6-migration.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))
    engine.dispose()


if __name__ == "__main__":
    main()
