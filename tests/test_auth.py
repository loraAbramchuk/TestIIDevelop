import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.application import create_app
from app.auth_dependencies import require_roles
from app.config import Settings
from app.models import AuthRateLimit, AuthSession, User
from app.security import token_hash, verify_password

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("RUN_DB_TESTS") != "1", reason="Set RUN_DB_TESTS=1 for PostgreSQL"
    ),
]
PASSWORD = "a long test password!"


@pytest.fixture
def auth_app(tmp_path):
    app = create_app(Settings(media_root=tmp_path / "media"))
    # Endpoints commit normally, but all writes remain in an outer test transaction.
    with app.state.engine.connect() as connection:
        transaction = connection.begin()
        app.state.session_factory = sessionmaker(
            bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )
        try:
            yield app
        finally:
            transaction.rollback()
    app.state.engine.dispose()


@pytest.fixture
def auth_client(auth_app):
    with TestClient(auth_app, base_url="https://testserver") as client:
        yield client


def csrf(client):
    response = client.get("/api/auth/csrf")
    assert response.status_code == 200, response.text
    return {"X-CSRF-Token": response.json()["csrf_token"]}


def register(client, **overrides):
    payload = {
        "email": f"{uuid.uuid4().hex}@example.com",
        "password": PASSWORD,
        "display_name": "Автор",
    } | overrides
    return client.post("/api/auth/register", json=payload, headers=csrf(client))


def test_registration_session_rotation_and_logout(auth_client, auth_app):
    headers = csrf(auth_client)
    anonymous = auth_client.cookies.get("video_session")
    assert csrf(auth_client) == headers
    assert auth_client.get("/api/auth/me").status_code == 401
    response = register(auth_client, email="  AUTHOR@Example.com ", display_name="  Автор  ")
    assert response.status_code == 201, response.text
    assert response.json()["user"]["email"] == "author@example.com"
    assert response.json()["user"]["display_name"] == "Автор"
    assert response.json()["user"]["role"] == "user"
    assert "password" not in response.text
    token = auth_client.cookies.get("video_session")
    assert token != anonymous
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie and "Path=/" in cookie
    assert auth_client.get("/api/auth/me").status_code == 200
    home = auth_client.get("/")
    assert "Автор" in home.text and "Выйти" in home.text
    assert "no-store" in home.headers["cache-control"]
    with auth_app.state.session_factory() as db:
        user = db.scalar(select(User).where(User.email == "author@example.com"))
        assert user.password_hash.startswith("$argon2id$")
        assert verify_password(PASSWORD, user.password_hash)
        assert db.get(AuthSession, token_hash(anonymous)) is None
        assert db.get(AuthSession, token_hash(token)) is not None
        assert db.get(AuthSession, token) is None
    # Old CSRF token is invalid after the login-session rotation.
    assert auth_client.post("/api/auth/logout", headers=headers).status_code == 403
    assert auth_client.post("/api/auth/logout", headers=csrf(auth_client)).status_code == 204
    assert auth_client.get("/api/auth/me").status_code == 401
    auth_client.cookies.set("video_session", token)
    assert auth_client.get("/api/auth/me").status_code == 401


def test_login_and_generic_errors(auth_client):
    assert register(auth_client, email="author@example.com").status_code == 201
    assert auth_client.post("/api/auth/logout", headers=csrf(auth_client)).status_code == 204
    headers = csrf(auth_client)
    wrong = auth_client.post(
        "/api/auth/login",
        headers=headers,
        json={"email": "author@example.com", "password": "wrong"},
    )
    missing = auth_client.post(
        "/api/auth/login",
        headers=headers,
        json={"email": "missing@example.com", "password": "wrong"},
    )
    assert wrong.status_code == missing.status_code == 401
    assert wrong.json() == missing.json()
    response = auth_client.post(
        "/api/auth/login",
        headers=headers,
        json={"email": "AUTHOR@example.com", "password": PASSWORD},
    )
    assert response.status_code == 200
    assert auth_client.get("/login", follow_redirects=False).status_code == 303


@pytest.mark.parametrize(
    "action,payload",
    [
        ("register", {"email": "test@example.com", "password": PASSWORD, "display_name": "Test"}),
        ("login", {"email": "test@example.com", "password": PASSWORD}),
        ("logout", None),
    ],
)
def test_csrf_required(auth_client, action, payload):
    assert auth_client.post(f"/api/auth/{action}", json=payload).status_code == 403
    csrf(auth_client)
    assert auth_client.post(f"/api/auth/{action}", json=payload).status_code == 403
    assert (
        auth_client.post(
            f"/api/auth/{action}", json=payload, headers={"X-CSRF-Token": "wrong"}
        ).status_code
        == 403
    )


@pytest.mark.parametrize(
    "changes", [{"role": "admin"}, {"password": "xY!7"}, {"display_name": "  "}, {"email": "bad"}]
)
def test_invalid_registration_never_echoes_password(auth_client, changes):
    response = register(auth_client, **changes)
    assert response.status_code == 422
    assert PASSWORD not in response.text and "xY!7" not in response.text
    assert all("input" not in error and "ctx" not in error for error in response.json()["detail"])


def test_unique_email_case_insensitive(auth_client):
    assert register(auth_client, email="author@example.com").status_code == 201
    auth_client.post("/api/auth/logout", headers=csrf(auth_client))
    assert register(auth_client, email="AUTHOR@EXAMPLE.COM").status_code == 409


def test_expired_session_is_rejected(auth_client, auth_app):
    assert register(auth_client).status_code == 201
    token = auth_client.cookies.get("video_session")
    with auth_app.state.session_factory() as db:
        session = db.get(AuthSession, token_hash(token))
        session.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()
    assert auth_client.get("/api/auth/me").status_code == 401
    assert auth_client.post("/api/auth/logout", headers={"X-CSRF-Token": "old"}).status_code == 403
    csrf(auth_client)
    assert auth_client.cookies.get("video_session") != token


def test_session_token_forgery_and_csrf_are_bound_to_session(auth_client, auth_app):
    first = csrf(auth_client)
    with TestClient(auth_app, base_url="https://testserver") as other:
        csrf(other)
        assert other.post("/api/auth/logout", headers=first).status_code == 403
    auth_client.cookies.set("video_session", "a" * 43)
    assert auth_client.get("/api/auth/me").status_code == 401


def test_role_guard_checks_fresh_role_from_database(auth_client, auth_app):
    @auth_app.get("/test-only/moderation")
    def moderation(user: Annotated[User, Depends(require_roles("moderator", "admin"))]):
        return {"role": user.role}

    assert auth_client.get("/test-only/moderation").status_code == 401
    result = register(auth_client)
    assert result.status_code == 201
    assert auth_client.get("/test-only/moderation").status_code == 403
    user_id = uuid.UUID(result.json()["user"]["id"])
    for role in ("moderator", "admin", "user"):
        with auth_app.state.session_factory() as db:
            db.get(User, user_id).role = role
            db.commit()
        assert auth_client.get("/test-only/moderation").status_code == (
            403 if role == "user" else 200
        )


def test_login_rate_limit_and_retry_after(auth_client, auth_app):
    headers = csrf(auth_client)
    payload = {"email": "missing@example.com", "password": "incorrect"}
    for _ in range(10):
        assert auth_client.post("/api/auth/login", headers=headers, json=payload).status_code == 401
    response = auth_client.post("/api/auth/login", headers=headers, json=payload)
    assert response.status_code == 429 and int(response.headers["retry-after"]) > 0
    with auth_app.state.session_factory() as db:
        limits = db.scalars(select(AuthRateLimit)).all()
        for limit in limits:
            limit.resets_at = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()
    assert auth_client.post("/api/auth/login", headers=headers, json=payload).status_code == 401


def test_registration_limit_and_no_forwarded_ip_spoofing(auth_client):
    for _ in range(5):
        assert register(auth_client).status_code in (201, 409)
    # Already-authenticated attempts are counted too; arbitrary headers are ignored by the app.
    headers = csrf(auth_client) | {"X-Forwarded-For": "203.0.113.99"}
    response = auth_client.post(
        "/api/auth/register",
        headers=headers,
        json={"email": "new@example.com", "password": PASSWORD, "display_name": "New"},
    )
    assert response.status_code == 429


def test_secure_host_cookie(auth_client, auth_app):
    auth_app.state.settings.auth_cookie_secure = True
    response = auth_client.get("/api/auth/csrf")
    cookie = response.headers["set-cookie"]
    assert cookie.startswith("__Host-video_session=")
    assert "Secure" in cookie and "HttpOnly" in cookie and "Domain=" not in cookie
    assert register(auth_client).status_code == 201
    assert auth_client.get("/api/auth/me").status_code == 200
    schema = auth_client.get("/openapi.json").json()
    assert schema["components"]["securitySchemes"]["APIKeyCookie"]["name"] == "__Host-video_session"


def test_display_name_is_escaped_in_html(auth_client):
    name = "<script>alert(1)</script>"
    assert register(auth_client, display_name=name).status_code == 201
    response = auth_client.get("/")
    assert name not in response.text
    assert "&lt;script&gt;" in response.text


def test_anonymous_session_creation_is_limited(auth_client):
    for _ in range(60):
        auth_client.cookies.clear()
        assert auth_client.get("/api/auth/csrf").status_code == 200
    auth_client.cookies.clear()
    response = auth_client.get("/api/auth/csrf")
    assert response.status_code == 429
    assert int(response.headers["retry-after"]) > 0


def test_admin_bootstrap_does_not_elevate_existing_user(auth_client, auth_app, monkeypatch, capsys):
    import sys

    from app import cli

    assert register(auth_client, email="existing@example.com").status_code == 201
    monkeypatch.setattr(cli, "make_engine", lambda settings: auth_app.state.engine)
    monkeypatch.setattr(cli, "make_session_factory", lambda engine: auth_app.state.session_factory)

    def run(email):
        monkeypatch.setattr(
            sys, "argv", ["cli", "create-admin", "--email", email, "--name", "Admin"]
        )
        monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: PASSWORD)
        cli.main()

    run("admin@example.com")
    with pytest.raises(SystemExit) as error:
        run("existing@example.com")
    assert error.value.code == 1
    with auth_app.state.session_factory() as db:
        assert db.scalar(select(User).where(User.email == "admin@example.com")).role == "admin"
        assert db.scalar(select(User).where(User.email == "existing@example.com")).role == "user"
    captured = capsys.readouterr()
    assert PASSWORD not in captured.out + captured.err
