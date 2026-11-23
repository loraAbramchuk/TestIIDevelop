import os
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
from app.security import hash_password, verify_password
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
