"""SIGKILL an API writer and reconcile its durable lease. Requires disposable *_probe DB."""

import json
import os
import secrets
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

import httpx
from sqlalchemy import func, select, text, update

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Settings  # noqa: E402
from app.db import make_engine, make_session_factory  # noqa: E402
from app.models import UploadAttempt, Video  # noqa: E402
from app.uploads.storage import attempt_key  # noqa: E402


def wait_for(predicate):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.05)
    raise AssertionError("Probe timed out")


def main():
    settings = Settings()
    if not settings.postgres_db.endswith("_probe"):
        raise SystemExit("Refusing crash probe: database name must end with _probe")
    engine = make_engine(settings)
    sessions = make_session_factory(engine)
    base = "http://127.0.0.1:8013"
    process = None
    log = open("/tmp/stage3-crash-api.log", "w")

    def start():
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                "8013",
                "--no-proxy-headers",
            ],
            stdout=log,
            stderr=log,
            env=os.environ.copy(),
        )

        def ready():
            assert process.poll() is None, "API exited; see /tmp/stage3-crash-api.log"
            try:
                return httpx.get(base + "/health/ready", trust_env=False).status_code == 200
            except httpx.TransportError:
                return False

        wait_for(ready)
        return process

    try:
        process = start()
        with httpx.Client(base_url=base, trust_env=False) as client:
            csrf = client.get("/api/auth/csrf").json()["csrf_token"]
            registered = client.post(
                "/api/auth/register",
                headers={"X-CSRF-Token": csrf},
                json={
                    "email": f"crash-{uuid4().hex}@example.com",
                    "display_name": "Crash probe",
                    "password": secrets.token_urlsafe(24),
                },
            )
            assert registered.status_code == 201, registered.text
            csrf = client.get("/api/auth/csrf").json()["csrf_token"]

            def create():
                response = client.post(
                    "/api/videos", headers={"X-CSRF-Token": csrf}, json={"title": "Crash probe"}
                )
                assert response.status_code == 201, response.text
                return UUID(response.json()["id"])

            accepted = create()
            assert (
                client.put(
                    f"/api/videos/{accepted}/content",
                    content=b"durable-source",
                    headers={"X-CSRF-Token": csrf, "Content-Type": "application/octet-stream"},
                ).status_code
                == 202
            )
            interrupted = create()

            def receiving():
                with sessions() as db:
                    return db.scalar(
                        select(UploadAttempt).where(
                            UploadAttempt.video_id == interrupted,
                            UploadAttempt.state == "receiving",
                        )
                    )

            def partial():
                yield b"incomplete"

                def on_disk():
                    row = receiving()
                    return (
                        row
                        and (settings.media_root / "uploads" / row.id.hex / "source.part").exists()
                    )

                wait_for(on_disk)
                process.kill()
                process.wait(timeout=5)

            try:
                client.put(
                    f"/api/videos/{interrupted}/content",
                    content=partial(),
                    headers={"X-CSRF-Token": csrf, "Content-Type": "application/octet-stream"},
                )
            except httpx.TransportError:
                pass
            assert process.returncode == -9
            stale = receiving()
            assert stale is not None
            assert (settings.media_root / "uploads" / stale.id.hex / "source.part").exists()
            # Avoid waiting the production 120s lease; simulate elapsed database time.
            with sessions() as db:
                db.execute(
                    update(UploadAttempt)
                    .where(UploadAttempt.id == stale.id)
                    .values(lease_until=func.clock_timestamp() - timedelta(seconds=1))
                )
                db.commit()
            subprocess.run([sys.executable, "-m", "app.uploads.maintenance", "--once"], check=True)
            with sessions() as db:
                row = db.get(UploadAttempt, stale.id)
                assert row.state == "cleaned" and row.reserved_bytes == 0
                assert db.get(Video, interrupted).error_code == "upload_expired"
                accepted_row = db.scalar(
                    select(UploadAttempt).where(UploadAttempt.video_id == accepted)
                )
                assert accepted_row.state == "processing"
                assert (
                    db.scalar(
                        text("SELECT count(*) FROM video_queue.procrastinate_jobs WHERE id=:id"),
                        {"id": accepted_row.job_id},
                    )
                    == 1
                )
                assert (
                    settings.media_root / attempt_key(accepted_row.id)
                ).read_bytes() == b"durable-source"
            assert not (settings.media_root / "uploads" / stale.id.hex).exists()
            process = start()
            assert (
                client.get(f"/api/videos/{accepted}/status").json()["processing_status"]
                == "processing"
            )
            assert (
                client.get(f"/api/videos/{interrupted}/status").json()["processing_status"]
                == "failed"
            )
            print(
                json.dumps(
                    {
                        "result": "passed",
                        "checks": [
                            "SIGKILL during body",
                            "orphan part persisted",
                            "expired lease reconciled",
                            "quota released after unlink",
                            "accepted source/job survived restart",
                        ],
                    }
                )
            )
    finally:
        if process and process.poll() is None:
            process.terminate()
            process.wait(timeout=10)
        log.close()
        engine.dispose()


if __name__ == "__main__":
    main()
