"""SIGKILL a real media worker. Run only on an empty, migrated disposable *_probe DB."""

import hashlib
import json
import os
import secrets
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import func, select, text  # noqa: E402

from app.application import create_app  # noqa: E402
from app.config import Settings  # noqa: E402
from app.models import AuthSession, ProcessingRun, UploadAttempt, User, Video  # noqa: E402
from app.processing.storage import Slot  # noqa: E402
from app.security import csrf_token, token_hash  # noqa: E402


def wait_for(predicate, seconds=30):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.02)
    raise AssertionError("Processing crash probe timed out")


def media_children(parent):
    result = []
    for path in Path("/proc").iterdir():
        if not path.name.isdigit():
            continue
        try:
            state = (path / "stat").read_text().split()
            command = (path / "cmdline").read_bytes().split(b"\0")
            if (
                int(state[3]) == parent
                and state[2] != "Z"
                and command
                and Path(os.fsdecode(command[0])).name in ("ffmpeg", "ffprobe")
            ):
                result.append(int(path.name))
        except (OSError, ValueError, IndexError):
            continue
    return result


def main():
    settings = Settings()
    if not settings.postgres_db.endswith("_probe"):
        raise SystemExit("Refusing: use an empty migrated disposable *_probe database")
    app = create_app(settings)
    sessions = app.state.session_factory
    with sessions() as db:
        if db.scalar(select(func.count()).select_from(UploadAttempt)):
            raise SystemExit("Refusing: probe database already contains upload attempts")
    log = open("/tmp/stage4-crash-worker.log", "w")
    child = None
    try:
        fixture = Path("/tmp/stage4-crash-input.mp4")
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-stream_loop",
                "49",
                "-i",
                "/tmp/stage1-media/h264-aac.mp4",
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                str(fixture),
            ],
            check=True,
            timeout=30,
        )
        payload = fixture.read_bytes()
        token = secrets.token_urlsafe(32)
        with sessions() as db:
            user = User(
                email=f"crash-{secrets.token_hex(8)}@example.com",
                display_name="Processing crash probe",
                password_hash="unused",
            )
            db.add(user)
            db.flush()
            db.add(
                AuthSession(
                    token_hash=token_hash(token),
                    user_id=user.id,
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                )
            )
            db.commit()
        with TestClient(app) as client:
            headers = {"Cookie": f"video_session={token}", "X-CSRF-Token": csrf_token(token)}
            response = client.post("/api/videos", json={"title": "Crash probe"}, headers=headers)
            assert response.status_code == 201, response.text
            from uuid import UUID

            video_id = UUID(response.json()["id"])
            response = client.put(
                f"/api/videos/{video_id}/content",
                content=payload,
                headers=headers | {"Content-Type": "application/octet-stream"},
            )
            assert response.status_code == 202, response.text

        def state():
            with sessions() as db:
                return db.get(Video, video_id).processing_status

        def start():
            return subprocess.Popen(
                [sys.executable, "-m", "app.processing.worker"], stdout=log, stderr=log
            )

        child = start()
        children = wait_for(lambda: media_children(child.pid))
        child.kill()
        child.wait(timeout=5)
        assert child.returncode == -signal.SIGKILL

        # Parent-death signal kills media tools; inherited locks also prevent overlapping writers.
        def media_stopped():
            for pid in children:
                try:
                    if Path(f"/proc/{pid}/stat").read_text().split()[2] != "Z":
                        return False
                except FileNotFoundError:
                    pass
            return True

        wait_for(media_stopped, seconds=5)
        slot = Slot(settings.media_root, settings.processing_concurrency)
        slot.close()
        assert state() == "processing"
        time.sleep(settings.processing_lease_seconds + 0.2)
        subprocess.run(
            [sys.executable, "-m", "app.uploads.maintenance", "--once"], check=True, timeout=15
        )
        with sessions() as db:
            assert (
                db.scalar(
                    select(func.count())
                    .select_from(ProcessingRun)
                    .where(ProcessingRun.state == "cleanup")
                )
                == 1
            )
        child = start()
        wait_for(lambda: state() == "ready", seconds=30)
        with sessions() as db:
            video = db.get(Video, video_id)
            attempt = db.get(UploadAttempt, video.current_upload_id)
            assert attempt.processing_attempts == 2
            assert (
                hashlib.sha256((settings.media_root / video.source_key).read_bytes()).digest()
                == hashlib.sha256(payload).digest()
            )
            assert (settings.media_root / video.playback_key).stat().st_size == video.playback_bytes
            assert (settings.media_root / video.thumbnail_key).read_bytes().startswith(b"\xff\xd8")
            job_id = attempt.job_id

        def acknowledged():
            with sessions() as db:
                return (
                    db.scalar(
                        text("SELECT status FROM video_queue.procrastinate_jobs WHERE id=:id"),
                        {"id": job_id},
                    )
                    == "succeeded"
                )

        wait_for(acknowledged)
        print(
            json.dumps(
                {
                    "result": "passed",
                    "checks": [
                        "SIGKILL during actual FFmpeg/ffprobe",
                        "media child terminated with parent",
                        "physical slot released",
                        "expired run fenced and job requeued",
                        "second generation publishes ready",
                        "source preserved",
                        "durable job succeeded",
                    ],
                }
            )
        )
    finally:
        if child and child.poll() is None:
            child.terminate()
            child.wait(timeout=15)
        log.close()
        app.state.engine.dispose()


if __name__ == "__main__":
    main()
