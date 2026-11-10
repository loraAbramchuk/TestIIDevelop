import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from app.application import create_app
from app.config import Settings
from app.models import User

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("RUN_DB_TESTS") != "1", reason="Set RUN_DB_TESTS=1 for PostgreSQL"
    ),
]


@pytest.fixture
def db_app(tmp_path):
    app = create_app(Settings(media_root=tmp_path / "media"))
    with TestClient(app):
        yield app


def test_migrated_database_is_ready(db_app):
    with TestClient(db_app) as client:
        response = client.get("/health/ready")
        assert response.status_code == 200
        assert response.json()["checks"] == {"database": "ok", "storage": "ok"}
    assert "users" in inspect(db_app.state.engine).get_table_names()


def test_user_defaults_unique_email_and_roles(db_app):
    # Every write rolls back, including a failed constraint check.
    with db_app.state.session_factory() as session, session.begin():
        email = f"{uuid.uuid4().hex}@example.test"
        user = User(email=email, display_name="Test", password_hash="not-a-real-login")
        session.add(user)
        session.flush()
        assert user.role == "user"
        assert user.id is not None and user.created_at is not None
        with pytest.raises(IntegrityError), session.begin_nested():
            session.add(User(email=email, display_name="Duplicate", password_hash="unused"))
            session.flush()
        with pytest.raises(IntegrityError), session.begin_nested():
            session.add(
                User(
                    email="invalid@example.test", display_name="Bad", password_hash="x", role="root"
                )
            )
            session.flush()
        with pytest.raises(IntegrityError), session.begin_nested():
            session.add(User(email="UPPER@example.test", display_name="Bad", password_hash="x"))
            session.flush()
        session.rollback()
