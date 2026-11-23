"""Verify 0010/0011 index migration preserves all old data in an empty *_probe DB."""

import hashlib
import json
import secrets
import subprocess
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import inspect, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.accounts import add_user_with_channel  # noqa: E402
from app.application import create_app  # noqa: E402
from app.config import Settings  # noqa: E402
from app.db import make_engine, make_session_factory  # noqa: E402
from app.models import (
    AuditEvent,
    AuthSession,
    Comment,
    Like,
    Playlist,
    PlaylistItem,
    Report,
    Video,
    VideoGrant,
)  # noqa: E402
from app.security import token_hash  # noqa: E402


def migrate(*args):
    subprocess.run(
        [sys.executable, "-m", "alembic", *args], check=True, capture_output=True, timeout=60
    )


def main():
    settings = Settings()
    assert settings.postgres_db.endswith("_probe")
    engine = make_engine(settings)
    assert not inspect(engine).has_table("users"), "Database must be empty"
    migrate("upgrade", "0010")
    root = Path(tempfile.mkdtemp(prefix="stage10-migration-"))
    media = Path("/tmp/stage1-media/h264-aac.mp4").read_bytes()
    for name in ("source.mp4", "playback.mp4"):
        (root / name).write_bytes(media)
    token = secrets.token_urlsafe(32)
    factory = make_session_factory(engine)
    with factory() as db:
        admin = add_user_with_channel(
            db,
            email="legacy-admin@example.com",
            display_name="Прежний администратор",
            role="admin",
            password_hash="preserved-admin-hash",
        )
        viewer = add_user_with_channel(
            db,
            email="legacy-viewer@example.com",
            display_name="Прежний модератор",
            role="moderator",
            password_hash="preserved-viewer-hash",
        )
        db.add(
            AuthSession(
                token_hash=token_hash(token),
                user_id=admin.id,
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )
        video = Video(
            owner_id=admin.id,
            title="Legacy private video",
            visibility="private",
            processing_status="ready",
            duration_seconds=2,
            width=320,
            height=180,
            source_key="source.mp4",
            source_bytes=len(media),
            playback_key="playback.mp4",
            playback_bytes=len(media),
        )
        db.add(video)
        db.flush()
        db.add(VideoGrant(video_id=video.id, user_id=viewer.id, granted_by=admin.id))
        db.add_all(
            [
                Like(video_id=video.id, user_id=viewer.id),
                Comment(video_id=video.id, user_id=viewer.id, text="Старый комментарий"),
                Report(
                    reporter_id=viewer.id,
                    video_id=video.id,
                    reason="other",
                    explanation="Старая жалоба",
                ),
            ]
        )
        playlist = Playlist(owner_id=viewer.id, title="Прежний список")
        db.add(playlist)
        db.flush()
        db.add(PlaylistItem(playlist_id=playlist.id, video_id=video.id, position=0))
        db.add(
            AuditEvent(
                actor_id=admin.id,
                object_type="video",
                object_id=video.id,
                action="video.restored",
                reason="Старая история",
                details={"previous_status": "hidden", "status": "visible"},
            )
        )
        video_id = video.id
        db.commit()

    def snapshot():
        result = {}
        with engine.connect() as conn:
            for schema in ("public", "video_queue"):
                for table in inspect(engine).get_table_names(schema=schema):
                    if table == "alembic_version":
                        continue
                    quoted = engine.dialect.identifier_preparer.quote
                    table_name = quoted(schema) + "." + quoted(table)
                    result[schema + "." + table] = conn.scalar(
                        text(
                            "SELECT coalesce(jsonb_agg(data ORDER BY data::text),'[]'::jsonb) "
                            "FROM (SELECT to_jsonb(t) data FROM " + table_name + " AS t) s"
                        )
                    )
        result["files"] = [
            hashlib.sha256((root / name).read_bytes()).hexdigest()
            for name in ("source.mp4", "playback.mp4")
        ]
        return result

    before = snapshot()
    for action in [
        ("upgrade", "head"),
        ("upgrade", "head"),
        ("downgrade", "0010"),
        ("upgrade", "head"),
    ]:
        migrate(*action)
        assert snapshot() == before, action
    migrate("check")
    with TestClient(create_app(settings.model_copy(update={"media_root": root}))) as client:
        headers = {"Cookie": f"video_session={token}"}
        result = client.get("/api/admin/users", headers=headers)
        assert result.status_code == 200 and result.json()["admin_count"] == 1
        assert client.get(f"/api/videos/{video_id}").status_code == 404
        assert client.get(f"/api/videos/{video_id}", headers=headers).status_code == 200
        assert client.get("/api/admin/audit", headers=headers).json()["items"] == []
        assert len(client.get("/api/moderation/audit", headers=headers).json()["items"]) == 1
    assert snapshot() == before
    report = {
        "result": "passed",
        "checks": [
            "0010 -> head -> repeated head -> 0010 -> head",
            "all public/video_queue rows and media hashes preserved",
            "all legacy domain data and history preserved",
            "old admin session accesses new panel without granting private access",
            "downgrade drops indexes only; history retained",
            "alembic check clean",
        ],
    }
    Path("/tmp/stage10-migration.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))
    engine.dispose()


if __name__ == "__main__":
    main()
