import os
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.accounts import add_user_with_channel
from app.config import Settings
from app.db import make_engine
from app.models import (
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
from app.security import hash_password, token_hash, verify_password
from app.seed import ACCOUNTS, PASSWORD, SeedError, add_examples, identifier, prepare_records

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_DB_TESTS") != "1", reason="Requires PostgreSQL"),
]


@pytest.fixture
def sessions():
    engine = make_engine(Settings())
    with engine.connect() as connection:
        transaction = connection.begin()
        factory = sessionmaker(
            bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )
        try:
            yield factory
        finally:
            transaction.rollback()
    engine.dispose()


def test_seed_is_repeatable_and_accounts_can_login_without_repair(sessions):
    prepare_records(sessions)
    add_examples(sessions)
    models = (
        User,
        Channel,
        Video,
        VideoGrant,
        Like,
        Comment,
        Subscription,
        Playlist,
        PlaylistItem,
        Report,
    )
    with sessions() as db:
        before = [db.scalar(select(func.count()).select_from(model)) for model in models]
        for key, email, role, _ in ACCOUNTS:
            user = db.get(User, identifier(key))
            assert user.email == email and user.role == role and user.email_verified
            assert verify_password(PASSWORD, user.password_hash)
        user = db.get(User, identifier("admin"))
        user.password_hash = hash_password("My changed password!2026")
        user.role = "user"
        video = db.get(Video, identifier("video:welcome"))
        video.title = "Changed by the reviewer"
        video.visibility = "private"
        db.commit()
    prepare_records(sessions)
    add_examples(sessions)
    with sessions() as db:
        assert before == [db.scalar(select(func.count()).select_from(model)) for model in models]
        user = db.get(User, identifier("admin"))
        assert user.role == "user" and verify_password(
            "My changed password!2026", user.password_hash
        )
        assert db.get(Video, identifier("video:welcome")).title == "Changed by the reviewer"
        assert db.get(Video, identifier("video:welcome")).visibility == "private"


def test_seed_refuses_to_elevate_or_reset_an_existing_account(sessions):
    with sessions() as db:
        existing = add_user_with_channel(
            db,
            id=uuid4(),
            email="admin@demo.example.com",
            display_name="Existing",
            role="user",
            password_hash=hash_password("Existing password!2026"),
        )
        existing_id = existing.id
        db.commit()
        before = db.scalar(select(func.count()).select_from(User))
    with pytest.raises(SeedError, match="nothing overwritten"):
        prepare_records(sessions)
    with sessions() as db:
        user = db.get(User, existing_id)
        assert user.role == "user" and verify_password("Existing password!2026", user.password_hash)
        assert db.scalar(select(func.count()).select_from(User)) == before


def test_seed_session_survives_a_long_queue_and_is_removed_afterwards(
    sessions, tmp_path, monkeypatch
):
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    from fastapi.testclient import TestClient

    from app import auth_dependencies, seed
    from app.application import create_app
    from app.models import AuthSession

    started = datetime.now(UTC)
    elapsed = 0
    with sessions() as db:
        db.add(
            AuthSession(
                token_hash=token_hash(str(uuid4())),
                expires_at=started + timedelta(hours=1),
            )
        )
        db.commit()
        previous_sessions = set(db.scalars(select(AuthSession.token_hash)))

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return started + timedelta(seconds=elapsed)

    class Engine:
        def connect(self):
            return self

        def execution_options(self, **kwargs):
            return self

        def __enter__(self):
            return SimpleNamespace(scalar=lambda *_: True, execute=lambda *_: None)

        def __exit__(self, *_):
            pass

        def dispose(self):
            pass

    settings = Settings(
        _env_file=None,
        postgres_password="seed-session-test-password",
        media_root=tmp_path / "media",
        auth_require_verified_email=True,
    )
    app = create_app(settings)
    app.state.session_factory = sessions
    monkeypatch.setattr(seed, "make_engine", lambda *_: Engine())
    monkeypatch.setattr(seed, "make_session_factory", lambda *_: sessions)
    monkeypatch.setattr(seed, "datetime", Clock)
    monkeypatch.setattr(auth_dependencies, "datetime", Clock)
    monkeypatch.setattr(
        seed.subprocess,
        "run",
        lambda args, **kw: Path(args[-1]).write_bytes(b"test-media"),
    )
    polls = []
    with TestClient(app) as client:

        def api(_base, path, *, token=None, method="GET", **kwargs):
            nonlocal elapsed
            if path == "/health/diagnostics":
                return {"status": "ok"}
            if method == "PUT":
                return {}
            # Four jobs finish just before their individual 600-second limit.
            # Exercise real cookie/session authentication after advancing UTC.
            elapsed += 590
            client.cookies.set("video_session", token)
            response = client.get("/api/auth/me")
            assert response.status_code == 200
            polls.append(path)
            return {"processing_status": "ready"}

        monkeypatch.setattr(seed, "api", api)
        seed.populate(settings, "http://127.0.0.1:8000", timeout=600)
    assert elapsed > 600 and len(polls) == 4
    with sessions() as db:
        assert set(db.scalars(select(AuthSession.token_hash))) == previous_sessions
