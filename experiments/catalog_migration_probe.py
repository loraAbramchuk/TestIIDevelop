"""Verify a catalog/interactions upgrade and downgrade only in an empty *_probe DB."""

import argparse
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
from app.models import (
    AuthSession,
    Channel,
    Comment,
    Like,
    ProcessingRun,
    Subscription,
    UploadAttempt,
    User,
    Video,
    VideoGrant,
)  # noqa: E402
from app.security import csrf_token, token_hash  # noqa: E402
from app.uploads.queue import enqueue_processing  # noqa: E402


def migrate(*arguments):
    subprocess.run(
        [sys.executable, "-m", "alembic", *arguments], check=True, capture_output=True, timeout=60
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", choices=("0006", "0007", "0008"), default="0006")
    baseline = parser.parse_args().baseline
    stage = {"0006": "6", "0007": "7", "0008": "8"}[baseline]
    settings = Settings()
    assert settings.postgres_db.endswith("_probe"), "Use a dedicated empty *_probe database"
    engine = make_engine(settings)
    assert not inspect(engine).has_table("users"), "Database must be empty"
    migrate("upgrade", baseline)
    factory = make_session_factory(engine)
    root = Path(tempfile.mkdtemp(prefix=f"stage{stage}-migration-"))
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
        if baseline in ("0007", "0008"):
            channel = Channel(
                owner_id=owner_id, name=owner.display_name, description="Старый профиль"
            )
            db.add_all([channel, Channel(owner_id=recipient_id, name=recipient.display_name)])
            db.flush()
            db.add(Subscription(subscriber_id=recipient_id, channel_id=channel.id))
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
        if baseline == "0008":
            db.add(Like(video_id=video_id, user_id=owner_id))
            db.add(Comment(video_id=video_id, user_id=recipient_id, text="Прежний комментарий"))
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
            result = {
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
            if baseline in ("0007", "0008"):
                result["channels"] = [
                    (str(c.id), str(c.owner_id), c.name, c.description)
                    for c in db.scalars(select(Channel).order_by(Channel.id))
                ]
                result["subscriptions"] = [
                    (str(s.subscriber_id), str(s.channel_id), s.created_at)
                    for s in db.scalars(select(Subscription))
                ]
            if baseline == "0008":
                result["likes"] = [
                    (str(r.video_id), str(r.user_id), r.created_at)
                    for r in db.scalars(select(Like))
                ]
                result["comments"] = [
                    (str(r.id), str(r.video_id), str(r.user_id), r.text, r.created_at)
                    for r in db.scalars(select(Comment))
                ]
            return result

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
        if baseline == "0007":
            likes_url = f"/api/videos/{video_id}/likes"
            comments_url = f"/api/videos/{video_id}/comments"
            assert client.get(likes_url, headers=recipient_headers).json()["like_count"] == 0
            assert client.get(comments_url, headers=recipient_headers).json()["total_count"] == 0
            writer = recipient_headers | {"X-CSRF-Token": csrf_token(tokens[1])}
            assert client.put(likes_url, headers=writer).json()["like_count"] == 1
            assert (
                client.post(
                    comments_url, headers=writer, json={"text": "После миграции"}
                ).status_code
                == 201
            )
            assert client.get(likes_url).status_code == 404
            assert client.get(comments_url).status_code == 404
        if baseline == "0008":
            writer = recipient_headers | {"X-CSRF-Token": csrf_token(tokens[1])}
            created = client.post(
                "/api/playlists", headers=writer, json={"title": "Новый плейлист"}
            )
            assert created.status_code == 201
            playlist_id = created.json()["id"]
            path = f"/api/playlists/{playlist_id}"
            assert client.put(path + f"/items/{video_id}", headers=writer).status_code == 204
            assert client.get(path, headers=recipient_headers).json()["visible_count"] == 1
            assert client.get(path).json()["visible_count"] == 0
            assert client.get(path + "/items").json()["items"] == []
    migrate("check")
    migrate("downgrade", baseline)
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
            f"downgrade/re-upgrade preserves baseline {baseline} data",
            "alembic check clean",
        ],
    }
    if baseline == "0007":
        with TestClient(create_app(settings)) as client:
            assert client.get(likes_url, headers=recipient_headers).json()["like_count"] == 0
            assert client.get(comments_url, headers=recipient_headers).json()["total_count"] == 0
        report["checks"].extend(
            [
                "channel profiles, IDs and subscriptions preserved",
                "legacy session uses new interactions with private ACL",
                "downgrade removes only new interaction data",
            ]
        )
    if baseline == "0008":
        with TestClient(create_app(settings)) as client:
            assert client.get(path).status_code == 404
            assert (
                client.get(f"/api/videos/{video_id}/likes", headers=recipient_headers).json()[
                    "like_count"
                ]
                == 1
            )
            assert (
                client.get(f"/api/videos/{video_id}/comments", headers=recipient_headers).json()[
                    "total_count"
                ]
                == 1
            )
        report["checks"].extend(
            [
                "channel IDs, profiles, subscriptions, likes and comments preserved",
                "legacy session uses playlists without granting private access",
                "downgrade removes only new playlist data",
            ]
        )
    Path(f"/tmp/stage{stage}-migration.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))
    engine.dispose()


if __name__ == "__main__":
    main()
