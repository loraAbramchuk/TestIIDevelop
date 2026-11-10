import pytest
from fastapi.testclient import TestClient

from app.application import create_app
from app.config import Settings


@pytest.fixture
def app(tmp_path):
    settings = Settings(
        _env_file=None,
        postgres_password="unit-test-password-only",
        db_host="127.0.0.1",
        db_port=1,
        media_root=tmp_path / "media",
    )
    return create_app(settings)


@pytest.fixture
def client(app):
    with TestClient(app) as client:
        yield client
