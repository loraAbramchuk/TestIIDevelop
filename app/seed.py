"""Explicit local demo data; media goes through the real upload and worker pipeline."""

import argparse
import json
import secrets
import subprocess
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from uuid import UUID, uuid5

from sqlalchemy import delete, select, text
from sqlalchemy.exc import SQLAlchemyError

from app.accounts import add_user_with_channel
from app.config import Settings
from app.db import make_engine, make_session_factory
from app.mail.policy import LOOPBACK
from app.models import (
    AuthSession,
    Channel,
    Comment,
    Like,
    Playlist,
    PlaylistItem,
    Report,
    Subscription,
    User,
    Video,
    VideoGrant,
)
from app.security import csrf_token, hash_password, token_hash

NAMESPACE = UUID("ec7dc389-fae8-4f03-939c-0b57a4a50fa2")
PASSWORD = "DemoVideo2026!"
ACCOUNTS = (
    ("creator", "creator@demo.example.com", "user", "Автор демо"),
    ("viewer", "viewer@demo.example.com", "user", "Зритель демо"),
    ("moderator", "moderator@demo.example.com", "moderator", "Модератор демо"),
    ("admin", "admin@demo.example.com", "admin", "Администратор демо"),
)
VIDEOS = (
    ("welcome", "Знакомство с видеоплатформой", "public"),
    ("upload", "Как загрузить первое видео", "public"),
    ("private", "Приватное видео для приглашённого зрителя", "private"),
    ("report", "Видео с демонстрационной жалобой", "public"),
)


class SeedError(Exception):
    pass


def identifier(name):
    return uuid5(NAMESPACE, name)


def validate_local(settings, base_url):
    origin = urlsplit(base_url)
    if (
        not settings.local_mail
        or origin.scheme != "http"
        or origin.hostname not in LOOPBACK
        or origin.username
        or origin.password
        or origin.path not in ("", "/")
        or origin.query
        or origin.fragment
    ):
        raise SeedError("Demo seed requires local HTTP with Mailpit; production is not supported")


def prepare_records(sessions):
    """Stable IDs; never reset an existing password, role or video metadata."""
    with sessions() as db:
        for key, email, role, name in ACCOUNTS:
            user = db.get(User, identifier(key))
            if user is None:
                if db.scalar(select(User.id).where(User.email == email)):
                    raise SeedError(
                        "A demo email belongs to an existing account; nothing overwritten"
                    )
                add_user_with_channel(
                    db,
                    id=identifier(key),
                    email=email,
                    role=role,
                    display_name=name,
                    password_hash=hash_password(PASSWORD),
                    email_verified_at=datetime.now(UTC),
                )
            elif user.email != email:
                raise SeedError("A demo identifier belongs to another account; nothing overwritten")
        for key, title, visibility in VIDEOS:
            video = db.get(Video, identifier("video:" + key))
            if video is None:
                db.add(
                    Video(
                        id=identifier("video:" + key),
                        owner_id=identifier("creator"),
                        title=title,
                        description="Демонстрационный ролик: синтетические кадры и звук.",
                        visibility=visibility,
                    )
                )
            elif video.owner_id != identifier("creator"):
                raise SeedError("A demo video belongs to another author; nothing overwritten")
        db.commit()


def add_examples(sessions):
    with sessions() as db:
        viewer, creator = identifier("viewer"), identifier("creator")
        channel = db.scalar(select(Channel.id).where(Channel.owner_id == creator))
        private, welcome = identifier("video:private"), identifier("video:welcome")
        if not db.get(VideoGrant, (private, viewer)):
            db.add(VideoGrant(video_id=private, user_id=viewer, granted_by=creator))
        if not db.get(Subscription, (viewer, channel)):
            db.add(Subscription(subscriber_id=viewer, channel_id=channel))
        if not db.get(Like, (welcome, viewer)):
            db.add(Like(video_id=welcome, user_id=viewer))
        if not db.get(Comment, identifier("comment")):
            db.add(
                Comment(
                    id=identifier("comment"),
                    video_id=welcome,
                    user_id=viewer,
                    text="Демонстрационный комментарий: видео и звук работают!",
                )
            )
        playlist = identifier("playlist")
        if not db.get(Playlist, playlist):
            db.add(Playlist(id=playlist, owner_id=creator, title="Начните знакомство здесь"))
            db.flush()
            for position, key in enumerate(("welcome", "upload", "private")):
                db.add(
                    PlaylistItem(
                        playlist_id=playlist, video_id=identifier("video:" + key), position=position
                    )
                )
        report_video = identifier("video:report")
        if not db.get(Report, identifier("report")) and not db.scalar(
            select(Report.id).where(
                Report.video_id == report_video,
                Report.reporter_id == viewer,
                Report.status == "open",
            )
        ):
            db.add(
                Report(
                    id=identifier("report"),
                    video_id=report_video,
                    reporter_id=viewer,
                    reason="other",
                    explanation="Учебная жалоба для проверки модерации.",
                )
            )
        db.commit()


def api(base, path, *, token=None, method="GET", body=None):
    headers = {"Content-Type": "application/octet-stream"}
    if token:
        headers.update({"Cookie": "video_session=" + token, "X-CSRF-Token": csrf_token(token)})
    try:
        with urlopen(
            Request(base + path, data=body, headers=headers, method=method), timeout=15
        ) as r:
            return json.load(r)
    except HTTPError as exc:
        raise SeedError(
            f"Seed API returned HTTP {exc.code}; see readiness/worker diagnostics"
        ) from None
    except (URLError, TimeoutError, json.JSONDecodeError):
        raise SeedError("Seed API unavailable; start the application and worker first") from None


def populate(settings, base_url, timeout=90):
    validate_local(settings, base_url)
    diagnostic = api(base_url, "/health/diagnostics")
    if diagnostic.get("status") != "ok":
        problems = ", ".join(
            f"{name}={state}"
            for name, state in diagnostic.get("checks", {}).items()
            if state != "ok"
        )
        raise SeedError("Seed requires healthy storage and worker: " + problems)
    engine = make_engine(settings)
    sessions = make_session_factory(engine)
    token = secrets.token_urlsafe(32)
    try:
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as lock:
            if not lock.scalar(text("SELECT pg_try_advisory_lock(7800123)")):
                raise SeedError("Another demo seed is already running")
            try:
                with tempfile.TemporaryDirectory(prefix="video-demo-") as temporary:
                    sample = Path(temporary) / "sample.mp4"
                    try:
                        subprocess.run(
                            [
                                "ffmpeg",
                                "-v",
                                "error",
                                "-cpucount",
                                "1",
                                "-filter_threads",
                                "1",
                                "-f",
                                "lavfi",
                                "-i",
                                "testsrc2=size=640x360:rate=25",
                                "-f",
                                "lavfi",
                                "-i",
                                "sine=frequency=440:sample_rate=48000",
                                "-t",
                                "3",
                                "-c:v",
                                "libx264",
                                "-threads",
                                "1",
                                "-pix_fmt",
                                "yuv420p",
                                "-c:a",
                                "aac",
                                "-movflags",
                                "+faststart",
                                str(sample),
                            ],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            timeout=45,
                            check=True,
                        )
                    except (OSError, subprocess.SubprocessError):
                        raise SeedError(
                            "Could not generate the demo MP4; FFmpeg is required"
                        ) from None
                    prepare_records(sessions)
                    with sessions() as db:
                        db.add(
                            AuthSession(
                                token_hash=token_hash(token),
                                user_id=identifier("creator"),
                                expires_at=datetime.now(UTC) + timedelta(minutes=10),
                            )
                        )
                        db.commit()
                    for key, _, _ in VIDEOS:
                        video_id = identifier("video:" + key)
                        with sessions() as db:
                            video = db.get(Video, video_id)
                            if video.deleted_at or video.processing_status == "ready":
                                continue
                            needs_upload = video.processing_status in ("uploading", "failed")
                        if needs_upload:
                            api(
                                base_url,
                                f"/api/videos/{video_id}/content",
                                token=token,
                                method="PUT",
                                body=sample.read_bytes(),
                            )
                        deadline = time.monotonic() + timeout
                        while True:
                            status = api(base_url, f"/api/videos/{video_id}/status", token=token)
                            if status["processing_status"] == "ready":
                                break
                            if status["processing_status"] == "failed":
                                raise SeedError(
                                    "Demo processing failed; fix the worker and rerun seed"
                                )
                            if time.monotonic() >= deadline:
                                raise SeedError(
                                    "Demo processing timed out; rerun after worker recovery"
                                )
                            time.sleep(0.5)
                    add_examples(sessions)
            finally:
                lock.execute(text("SELECT pg_advisory_unlock(7800123)"))
    finally:
        try:
            with sessions() as db:
                db.execute(delete(AuthSession).where(AuthSession.token_hash == token_hash(token)))
                db.commit()
        finally:
            engine.dispose()


def main():
    parser = argparse.ArgumentParser(description="Fill a local demo with real playable videos")
    parser.add_argument("--demo", action="store_true", help="Explicitly enable local demo accounts")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--timeout", type=int, default=90)
    args = parser.parse_args()
    if not args.demo or not 1 <= args.timeout <= 600:
        parser.error("Use --demo for local data; --timeout must be 1–600 seconds")
    try:
        populate(Settings(), args.base_url.rstrip("/"), args.timeout)
    except SeedError as exc:
        parser.exit(1, str(exc) + "\n")
    except SQLAlchemyError:
        parser.exit(
            1, "Seed database unavailable or conflicts with existing data; no account reset\n"
        )
    print(
        "Demo ready: 4 accounts, 4 processed videos, playlist, grant, "
        "subscription, like, comment, report."
    )
    print("Initial demo password (existing passwords are preserved): " + PASSWORD)
    for _, email, role, _ in ACCOUNTS:
        print(role + ": " + email)


if __name__ == "__main__":
    main()
