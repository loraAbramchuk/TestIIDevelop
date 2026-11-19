import os
import secrets
import smtplib
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import sessionmaker

from app.application import create_app
from app.config import Settings
from app.mail import service
from app.models import AuthMailToken, AuthSession, User

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_DB_TESTS") != "1", reason="Requires PostgreSQL"),
]
PASSWORD = "Original mail password!123"
NEW_PASSWORD = "New mail password!1234"


@pytest.fixture
def mail_app(tmp_path):
    app = create_app(
        Settings(
            media_root=tmp_path / "media",
            auth_require_verified_email=True,
            auth_mail_secret=secrets.token_urlsafe(48),
            smtp_host="127.0.0.1",
            smtp_from="noreply@example.com",
            smtp_security="plain",
            smtp_port=1026,
            public_base_url="https://video.example.com",
        )
    )
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
def client(mail_app):
    with TestClient(mail_app, base_url="https://video.example.com") as client:
        yield client


def write(client, path, data=None):
    csrf = client.get("/api/auth/csrf").json()["csrf_token"]
    return client.post("/api/auth/" + path, json=data, headers={"X-CSRF-Token": csrf})


def register(client):
    email = uuid4().hex + "@example.com"
    response = write(
        client, "register", {"email": email, "password": PASSWORD, "display_name": "Автор"}
    )
    assert response.status_code == 201, response.text
    return email, response.json()["user"]


def record(app, purpose):
    with app.state.session_factory() as db:
        return db.scalar(
            select(AuthMailToken).where(
                AuthMailToken.purpose == purpose, AuthMailToken.used_at.is_(None)
            )
        )


def token(app, purpose):
    return service.token_value(app.state.settings, record(app, purpose).id, purpose)


def test_registration_verification_and_minimal_queue_payload(client, mail_app, monkeypatch):
    email, user = register(client)
    assert user["email_verified"] is False
    response = client.post(
        "/api/videos",
        json={"title": "Before verification"},
        headers={"X-CSRF-Token": client.get("/api/auth/csrf").json()["csrf_token"]},
    )
    assert response.status_code == 403 and response.json()["code"] == "email_not_verified"
    action = record(mail_app, "verify_email")
    value = token(mail_app, "verify_email")
    with mail_app.state.session_factory() as db:
        args = db.scalar(
            text(
                "SELECT args FROM video_queue.procrastinate_jobs "
                "WHERE task_name='auth.send' AND args->>'token_id'=:id"
            ),
            {"id": str(action.id)},
        )
        assert args == {"token_id": str(action.id)}
        assert value not in str(args) and email not in str(args)
        assert action.token_hash != value and PASSWORD not in str(args)
    messages = []

    class SMTP:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def send_message(self, message):
            messages.append(message)

    monkeypatch.setattr(service.smtplib, "SMTP", SMTP)
    service.deliver(mail_app.state.settings, mail_app.state.session_factory, str(action.id))
    assert messages[0]["To"] == email
    assert "https://video.example.com/verify-email#token=" + value in messages[0].get_content()
    service.deliver(mail_app.state.settings, mail_app.state.session_factory, str(action.id))
    assert len(messages) == 1
    assert write(client, "email/verify", {"token": value}).status_code == 200
    assert client.get("/api/auth/me").json()["email_verified"] is True
    assert write(client, "email/verify", {"token": value}).status_code == 400
    response = client.post(
        "/api/videos",
        json={"title": "Verified"},
        headers={"X-CSRF-Token": client.get("/api/auth/csrf").json()["csrf_token"]},
    )
    assert response.status_code == 201


def test_reset_is_one_use_invalidates_old_links_and_all_sessions(client, mail_app):
    email, user = register(client)
    old_cookie = client.cookies.get("video_session")
    old_verify = token(mail_app, "verify_email")
    assert write(client, "password/request", {"email": email}).status_code == 202
    older = token(mail_app, "reset_password")
    assert write(client, "password/request", {"email": email}).status_code == 202
    value = token(mail_app, "reset_password")
    assert value != older
    assert (
        write(client, "password/reset", {"token": older, "password": NEW_PASSWORD}).status_code
        == 400
    )
    assert (
        write(client, "password/reset", {"token": old_verify, "password": NEW_PASSWORD}).status_code
        == 400
    )
    assert (
        write(client, "password/reset", {"token": value, "password": NEW_PASSWORD}).status_code
        == 200
    )
    assert client.get("/api/auth/me").status_code == 401
    client.cookies.set("video_session", old_cookie)
    assert client.get("/api/auth/me").status_code == 401
    with mail_app.state.session_factory() as db:
        assert not db.scalars(select(AuthSession).where(AuthSession.user_id == user["id"])).all()
    client.cookies.clear()
    assert (
        write(client, "password/reset", {"token": value, "password": PASSWORD}).status_code == 400
    )
    assert write(client, "email/verify", {"token": old_verify}).status_code == 400
    assert write(client, "login", {"email": email, "password": PASSWORD}).status_code == 401
    assert write(client, "login", {"email": email, "password": NEW_PASSWORD}).status_code == 200
    assert client.get("/api/auth/me").json()["email_verified"] is True


def test_password_request_is_generic_and_not_automatic_login(client, mail_app):
    email, _ = register(client)
    known = write(client, "password/request", {"email": email})
    unknown = write(client, "password/request", {"email": "absent@example.com"})
    assert known.status_code == unknown.status_code == 202
    assert known.json() == unknown.json()
    assert email not in known.text and PASSWORD not in known.text


@pytest.mark.parametrize(
    "purpose,path", [("verify_email", "email/verify"), ("reset_password", "password/reset")]
)
def test_expired_and_tampered_links_fail_without_echo(client, mail_app, purpose, path):
    email, _ = register(client)
    if purpose == "reset_password":
        write(client, "password/request", {"email": email})
    value = token(mail_app, purpose)
    with mail_app.state.session_factory() as db:
        item = db.get(AuthMailToken, record(mail_app, purpose).id)
        item.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()
    for candidate in [value, value[:-1] + ("A" if value[-1] != "A" else "B"), "bad-token"]:
        payload = {"token": candidate}
        if purpose == "reset_password":
            payload["password"] = NEW_PASSWORD
        response = write(client, path, payload)
        assert response.status_code == 400
        assert response.json()["code"] == "action_token_invalid"
        assert candidate not in response.text and NEW_PASSWORD not in response.text


@pytest.mark.parametrize(
    "path,payload",
    [
        ("email/request", None),
        ("email/verify", {"token": "secret-token"}),
        ("password/request", {"email": "user@example.com"}),
        ("password/reset", {"token": "secret-token", "password": NEW_PASSWORD}),
    ],
)
def test_csrf_is_required(client, path, payload):
    assert client.post("/api/auth/" + path, json=payload).status_code == 403


def test_validation_never_echoes_password_or_token(client):
    response = write(client, "password/reset", {"token": "secret-token", "password": "Q9!abc"})
    assert response.status_code == 422
    assert "secret-token" not in response.text and "Q9!abc" not in response.text
    assert all("input" not in item and "ctx" not in item for item in response.json()["detail"])


def test_resend_invalidates_previous_verification_and_has_throttle(client, mail_app):
    register(client)
    old = token(mail_app, "verify_email")
    assert write(client, "email/request").status_code == 202
    assert token(mail_app, "verify_email") != old
    assert write(client, "email/verify", {"token": old}).status_code == 400
    for _ in range(4):
        assert write(client, "email/request").status_code == 202
    response = write(client, "email/request")
    assert response.status_code == 429 and response.headers.get("retry-after")


def test_smtp_error_is_redacted_and_retry_is_possible(client, mail_app, monkeypatch):
    register(client)
    action = record(mail_app, "verify_email")

    class SMTP:
        def __init__(self, *a, **kw):
            raise smtplib.SMTPRecipientsRefused(
                {"private@example.com": (550, b"private diagnostic")}
            )

    monkeypatch.setattr(service.smtplib, "SMTP", SMTP)
    with pytest.raises(service.MailDeliveryError) as error:
        service.deliver(mail_app.state.settings, mail_app.state.session_factory, str(action.id))
    assert "private" not in str(error.value)
    with mail_app.state.session_factory() as db:
        assert db.get(AuthMailToken, action.id).sent_at is None


def test_verification_policy_fails_safely_when_mail_is_not_configured(client, mail_app):
    mail_app.state.settings.smtp_host = ""
    response = write(
        client,
        "register",
        {"email": "not-created@example.com", "password": PASSWORD, "display_name": "Test"},
    )
    assert response.status_code == 503 and response.json()["code"] == "mail_unavailable"
    with mail_app.state.session_factory() as db:
        assert db.scalar(select(User).where(User.email == "not-created@example.com")) is None


@pytest.mark.parametrize("field", ["email_verified", "email_verified_at"])
def test_registration_cannot_self_confirm_address(client, field):
    response = write(
        client,
        "register",
        {
            "email": "not-created@example.com",
            "password": PASSWORD,
            "display_name": "Test",
            field: True,
        },
    )
    assert response.status_code == 422


@pytest.fixture
def committed_mail(tmp_path):
    from app.accounts import add_user_with_channel
    from app.security import hash_password

    app = create_app(
        Settings(
            media_root=tmp_path / "media",
            auth_require_verified_email=True,
            auth_mail_secret=secrets.token_urlsafe(48),
            smtp_host="127.0.0.1",
            smtp_security="plain",
            smtp_from="noreply@example.com",
        )
    )
    with app.state.session_factory() as db:
        user = add_user_with_channel(
            db,
            email=uuid4().hex + "@example.com",
            display_name="Concurrent reset",
            password_hash=hash_password(PASSWORD),
            role="user",
        )
        action = service.issue(db, app.state.settings, user, "reset_password")
        db.commit()
        value = service.token_value(app.state.settings, action.id, "reset_password")
    try:
        yield app, user, value
    finally:
        from sqlalchemy import delete

        with app.state.session_factory() as db:
            db.execute(
                text(
                    "DELETE FROM video_queue.procrastinate_jobs "
                    "WHERE task_name='auth.send' AND args->>'token_id'=:id"
                ),
                {"id": str(action.id)},
            )
            db.execute(delete(User).where(User.id == user.id))
            db.commit()
        app.state.engine.dispose()


def test_parallel_resets_consume_a_link_only_once(committed_mail):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from app.errors import DomainError

    app, user, value = committed_mail
    barrier = Barrier(2)

    def consume():
        barrier.wait(timeout=5)
        try:
            with app.state.session_factory() as db:
                service.consume(db, app.state.settings, value, "reset_password", NEW_PASSWORD)
            return 200
        except DomainError as error:
            return error.status

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: consume(), range(2)))
    assert sorted(results) == [200, 400]


def test_old_password_login_cannot_survive_a_concurrent_reset(committed_mail, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from app.api import auth

    app, user, value = committed_mail
    entered, release, resetting, hashed = Event(), Event(), Event(), Event()
    original = auth.verify_password

    def held_verify(*args):
        entered.set()
        assert release.wait(timeout=10)
        return original(*args)

    monkeypatch.setattr(auth, "verify_password", held_verify)
    from app.security import hash_password

    new_hash = hash_password(NEW_PASSWORD)

    def observed_hash(_password):
        hashed.set()
        return new_hash

    monkeypatch.setattr(service, "hash_password", observed_hash)
    with TestClient(
        app, base_url="https://testserver", client=("mail-race-" + uuid4().hex, 50000)
    ) as client:
        csrf = client.get("/api/auth/csrf").json()["csrf_token"]

        def reset():
            resetting.set()
            with app.state.session_factory() as db:
                service.consume(db, app.state.settings, value, "reset_password", NEW_PASSWORD)

        with ThreadPoolExecutor(max_workers=2) as pool:
            login = pool.submit(
                client.post,
                "/api/auth/login",
                headers={"X-CSRF-Token": csrf},
                json={"email": user.email, "password": PASSWORD},
            )
            assert entered.wait(timeout=5)
            changed = pool.submit(reset)
            try:
                assert resetting.wait(timeout=5)
                assert not hashed.wait(timeout=0.5)
            finally:
                release.set()
            assert login.result(timeout=15).status_code == 200
            changed.result(timeout=15)
        assert client.get("/api/auth/me").status_code == 401
