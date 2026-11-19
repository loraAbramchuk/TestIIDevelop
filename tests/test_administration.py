import json
import os
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, event, func, select, text

from app.accounts import add_user_with_channel
from app.administration.service import ROLE_LOCK_KEY, ROLE_LOCK_NAMESPACE
from app.application import create_app
from app.config import Settings
from app.models import AuditEvent, AuthSession, Channel, User, Video
from app.security import csrf_token, token_hash

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_DB_TESTS") != "1", reason="Requires PostgreSQL"),
]


@pytest.fixture
def administration(tmp_path):
    app = create_app(Settings(media_root=tmp_path / "media"))
    factory = app.state.session_factory
    actors = {}
    with factory() as db:
        for name, role in [
            ("admin", "admin"),
            ("other", "admin"),
            ("moderator", "moderator"),
            ("member", "user"),
        ]:
            user = add_user_with_channel(
                db,
                email=f"{uuid4().hex}@example.com",
                display_name=name,
                password_hash="unusable-test-hash",
                role=role,
            )
            token = secrets.token_urlsafe(32)
            db.add(
                AuthSession(
                    token_hash=token_hash(token),
                    user_id=user.id,
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                )
            )
            actors[name] = SimpleNamespace(
                id=user.id,
                headers={"Cookie": f"video_session={token}", "X-CSRF-Token": csrf_token(token)},
            )
        db.commit()
    with TestClient(app) as client:
        yield SimpleNamespace(app=app, factory=factory, actors=actors, client=client)
    with factory() as db:
        ids = [a.id for a in actors.values()]
        db.execute(delete(AuditEvent).where(AuditEvent.actor_id.in_(ids)))
        db.execute(delete(Video).where(Video.owner_id.in_(ids)))
        db.execute(delete(User).where(User.id.in_(ids)))
        db.commit()
    app.state.engine.dispose()


def change(c, target="member", role="moderator", actor="admin", **extra):
    return c.client.patch(
        f"/api/admin/users/{c.actors[target].id}/role",
        content=json.dumps({"role": role, "reason": "  Обоснование 🎬  "} | extra),
        headers=c.actors[actor].headers | {"Content-Type": "application/json"},
    )


@pytest.mark.parametrize(
    "actor,expected",
    [(None, 401), ("member", 403), ("moderator", 403), ("admin", 200), ("other", 200)],
)
def test_admin_routes_are_private_and_role_guarded(administration, actor, expected):
    c = administration
    headers = c.actors[actor].headers if actor else {}
    for path in [
        "/api/admin/users",
        f"/api/admin/users/{c.actors['member'].id}",
        "/api/admin/audit",
    ]:
        response = c.client.get(path, headers=headers)
        assert response.status_code == expected
        assert "no-store" in response.headers["cache-control"]
        assert "cookie" in response.headers["vary"].lower()
        assert "password_hash" not in response.text and "token_hash" not in response.text
    for path in ["/admin/users", "/admin/audit"]:
        response = c.client.get(path, headers=headers, follow_redirects=False)
        assert response.status_code == (303 if actor is None else expected)
    html = c.client.get("/", headers=headers).text
    assert ('href="/admin/users"' in html) is (expected == 200)


def test_role_change_is_audited_idempotent_and_immediate_for_existing_session(administration):
    c = administration
    result = change(c)
    assert result.status_code == 200 and result.json()["changed"] is True
    assert result.json()["user"]["role"] == "moderator" and result.json()["admin_count"] == 2
    assert (
        c.client.get("/api/auth/me", headers=c.actors["member"].headers).json()["role"]
        == "moderator"
    )
    assert (
        c.client.get("/api/moderation/reports", headers=c.actors["member"].headers).status_code
        == 200
    )
    repeated = change(c, actor="other", reason="Другой текст", expected_role="user")
    assert repeated.status_code == 200 and repeated.json()["changed"] is False
    with c.factory() as db:
        events = list(
            db.scalars(select(AuditEvent).where(AuditEvent.object_id == c.actors["member"].id))
        )
        assert len(events) == 1 and events[0].reason == "Обоснование 🎬"
        assert events[0].actor_id == c.actors["admin"].id
        assert events[0].details == {"previous_role": "user", "role": "moderator"}
    assert change(c, role="admin", expected_role="user").json()["code"] == "role_conflict"
    assert change(c, role="admin", expected_role="moderator").json()["admin_count"] == 3
    assert c.client.get("/api/admin/users", headers=c.actors["member"].headers).status_code == 200
    assert change(c, role="user").status_code == 200
    assert c.client.get("/api/admin/users", headers=c.actors["member"].headers).status_code == 403


def test_last_admin_is_protected_and_self_demotion_retains_session(administration):
    c = administration
    assert change(c, "other", "user").status_code == 200
    for role in ("user", "moderator"):
        result = change(c, "admin", role)
        assert result.status_code == 409 and result.json()["code"] == "last_admin"
    assert change(c, "admin", "admin").json()["changed"] is False
    assert change(c, "other", "admin").status_code == 200
    result = change(c, "admin", "moderator")
    assert result.json()["actor_role"] == "moderator"
    assert (
        c.client.get("/api/auth/me", headers=c.actors["admin"].headers).json()["role"]
        == "moderator"
    )
    assert c.client.get("/api/admin/users", headers=c.actors["admin"].headers).status_code == 403


@pytest.mark.parametrize("kind", ["self", "mutual", "same_target"])
def test_concurrent_role_changes_keep_an_admin_without_deadlocks_or_duplicate_audit(
    administration, kind
):
    c = administration
    with ThreadPoolExecutor(max_workers=5) as pool:
        if kind == "self":
            futures = [pool.submit(change, c, name, "user", name) for name in ("admin", "other")]
        elif kind == "mutual":
            futures = [
                pool.submit(change, c, "other", "user", "admin"),
                pool.submit(change, c, "admin", "user", "other"),
            ]
        else:
            futures = [pool.submit(change, c) for _ in range(5)]
        responses = [f.result(timeout=15) for f in futures]
    if kind == "same_target":
        assert all(r.status_code == 200 for r in responses)
        assert sum(r.json()["changed"] for r in responses) == 1
    else:
        assert sorted(r.status_code for r in responses) == (
            [200, 409] if kind == "self" else [200, 403]
        )
    with c.factory() as db:
        assert db.scalar(select(func.count()).select_from(User).where(User.role == "admin")) == (
            2 if kind == "same_target" else 1
        )
        assert (
            db.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.actor_id.in_([a.id for a in c.actors.values()]))
            )
            == 1
        )


@pytest.mark.parametrize("lock_kind", ["advisory", "actor"])
def test_waiting_role_writer_rechecks_revoked_actor_role(administration, lock_kind):
    c = administration
    with c.factory() as blocker, ThreadPoolExecutor(max_workers=1) as pool:
        if lock_kind == "advisory":
            blocker.execute(select(func.pg_advisory_xact_lock(ROLE_LOCK_NAMESPACE, ROLE_LOCK_KEY)))
        else:
            blocker.scalar(select(User).where(User.id == c.actors["admin"].id).with_for_update())
        future = pool.submit(change, c)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with c.factory() as observer:
                waiting = observer.scalar(
                    text("SELECT EXISTS (SELECT 1 FROM pg_locks WHERE NOT granted)")
                )
            if waiting:
                break
            time.sleep(0.01)
        assert waiting and not future.done()
        blocker.get(User, c.actors["admin"].id).role = "user"
        blocker.commit()
        assert future.result(timeout=10).status_code == 403
    with c.factory() as db:
        assert db.get(User, c.actors["member"].id).role == "user"
        assert (
            list(db.scalars(select(AuditEvent).where(AuditEvent.actor_id == c.actors["admin"].id)))
            == []
        )


def test_audit_failure_rolls_back_role_assignment(administration):
    c = administration

    def fail(*_):
        raise RuntimeError("injected audit failure")

    event.listen(AuditEvent, "before_insert", fail)
    try:
        with TestClient(c.app, raise_server_exceptions=False) as client:
            response = client.patch(
                f"/api/admin/users/{c.actors['member'].id}/role",
                json={"role": "admin", "reason": "Review"},
                headers=c.actors["admin"].headers,
            )
        assert response.status_code == 500
    finally:
        event.remove(AuditEvent, "before_insert", fail)
    with c.factory() as db:
        assert db.get(User, c.actors["member"].id).role == "user"
        assert (
            list(
                db.scalars(select(AuditEvent).where(AuditEvent.object_id == c.actors["member"].id))
            )
            == []
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("reason", ""),
        ("reason", "   "),
        ("reason", "x" * 2001),
        ("reason", "\x00"),
        ("reason", "\ud800"),
        ("reason", None),
        ("role", "owner"),
        ("role", None),
        ("expected_role", "root"),
        ("email", "forged@example.com"),
        ("password_hash", "forged"),
    ],
)
def test_role_payload_validation_and_server_owned_fields(administration, field, value):
    c = administration
    assert change(c, **{field: value}).status_code == 422


def test_role_writes_require_csrf_and_reject_public_privilege_assignment(administration):
    c = administration
    for actor in ("member", "moderator"):
        assert change(c, actor=actor).status_code == 403
    assert (
        c.client.patch(
            f"/api/admin/users/{c.actors['member'].id}/role",
            json={"role": "admin", "reason": "test"},
            headers={"Cookie": c.actors["admin"].headers["Cookie"]},
        ).status_code
        == 403
    )
    assert (
        c.client.post(
            "/api/auth/register",
            json={
                "email": "forged@example.com",
                "display_name": "Forged",
                "password": "valid-test-password",
                "role": "admin",
            },
            headers=c.actors["member"].headers,
        ).status_code
        == 422
    )
    with c.factory() as db:
        channel = db.scalar(select(Channel.id).where(Channel.owner_id == c.actors["member"].id))
    assert (
        c.client.patch(
            f"/api/channels/{channel}", json={"role": "admin"}, headers=c.actors["member"].headers
        ).status_code
        == 422
    )
    assert (
        c.client.patch(
            f"/api/admin/users/{uuid4()}/role",
            json={"role": "admin", "reason": "test"},
            headers=c.actors["admin"].headers,
        ).status_code
        == 404
    )


def test_role_history_is_admin_only_and_separated_from_content_moderation(administration):
    c = administration
    assert change(c).status_code == 200
    for actor in ("admin", "moderator", "member"):
        moderation = c.client.get("/api/moderation/audit", headers=c.actors[actor].headers)
        assert moderation.status_code == 200
        assert "user.role_changed" not in moderation.text and "Обоснование" not in moderation.text
    assert (
        c.client.get("/api/admin/audit", headers=c.actors["moderator"].headers).status_code == 403
    )
    result = c.client.get("/api/admin/audit", headers=c.actors["admin"].headers).json()["items"]
    assert len(result) == 1 and result[0]["object_id"] == str(c.actors["member"].id)


def test_promotion_never_grants_access_to_private_or_hidden_video(administration):
    c = administration
    with c.factory() as db:
        v = Video(
            owner_id=c.actors["other"].id,
            title="Private marker",
            visibility="private",
            processing_status="ready",
            duration_seconds=2,
            width=320,
            height=180,
        )
        db.add(v)
        db.commit()
        identifier = v.id
    for role in ("moderator", "admin"):
        assert change(c, role=role).status_code == 200
        for suffix in ("", "/stream", "/thumbnail", "/likes", "/comments"):
            assert (
                c.client.get(
                    f"/api/videos/{identifier}" + suffix, headers=c.actors["member"].headers
                ).status_code
                == 404
            )
        with c.factory() as db:
            assert db.get(User, c.actors["member"].id).role == role


def test_search_pagination_filters_and_context(administration):
    c = administration
    headers = c.actors["admin"].headers
    with c.factory() as db:
        db.get(User, c.actors["member"].id).display_name = "Unique %_ / \\ ИмЯ"
        db.commit()
    for query in ("%", "_", "/", "\\", "иМя"):
        result = c.client.get("/api/admin/users", params={"q": query}, headers=headers)
        assert result.status_code == 200
        assert [i["id"] for i in result.json()["items"]] == [str(c.actors["member"].id)]
    path = "/api/admin/users"
    cursor = None
    ids = []
    while True:
        data = c.client.get(
            path, params={"limit": 1} | ({"cursor": cursor} if cursor else {}), headers=headers
        ).json()
        ids.extend(i["id"] for i in data["items"])
        cursor = data["next_cursor"]
        if not cursor:
            break
    assert set(ids) == {str(a.id) for a in c.actors.values()} and len(ids) == 4
    first = c.client.get(path, params={"limit": 1}, headers=headers).json()["next_cursor"]
    for params, actor in [({"q": "changed"}, "admin"), ({"role": "admin"}, "admin"), ({}, "other")]:
        assert (
            c.client.get(
                path, params={"cursor": first} | params, headers=c.actors[actor].headers
            ).status_code
            == 400
        )
    assert len(c.client.get(path, params={"role": "admin"}, headers=headers).json()["items"]) == 2
    for params in ({"limit": 0}, {"limit": 101}, {"q": "x" * 201}, {"role": "root"}):
        assert c.client.get(path, params=params, headers=headers).status_code == 422
    assert c.client.get(path, params={"q": "\x00"}, headers=headers).status_code == 400


def test_write_limit_is_personal(administration):
    c = administration
    c.app.state.settings.admin_write_limit_per_minute = 1
    assert change(c).status_code == 200
    result = change(c, role="user")
    assert result.status_code == 429 and int(result.headers["retry-after"]) > 0
    assert change(c, role="user", actor="other").status_code == 200


@pytest.mark.parametrize("initial", ["user", "moderator", "admin"])
@pytest.mark.parametrize("desired", ["user", "moderator", "admin"])
def test_all_role_transitions_are_reported_correctly(administration, initial, desired):
    c = administration
    with c.factory() as db:
        db.get(User, c.actors["member"].id).role = initial
        db.commit()
    response = change(c, role=desired, expected_role=initial)
    assert response.status_code == 200 and response.json()["changed"] is (initial != desired)
    assert response.json()["user"]["role"] == desired
    with c.factory() as db:
        assert db.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.object_id == c.actors["member"].id)
        ) == int(initial != desired)


def test_role_audit_pagination_and_cursor_context(administration):
    c = administration
    headers = c.actors["admin"].headers
    for role in ("moderator", "user", "admin", "moderator", "user"):
        assert change(c, role=role).status_code == 200
    path = "/api/admin/audit"
    cursor = None
    ids = []
    while True:
        result = c.client.get(
            path, params={"limit": 1} | ({"cursor": cursor} if cursor else {}), headers=headers
        ).json()
        ids.extend(i["id"] for i in result["items"])
        cursor = result["next_cursor"]
        if not cursor:
            break
    assert len(ids) == len(set(ids)) == 5
    first = c.client.get(path, params={"limit": 1}, headers=headers).json()["next_cursor"]
    assert (
        c.client.get(path, params={"cursor": first}, headers=c.actors["other"].headers).status_code
        == 400
    )
    assert (
        c.client.get(
            path, params={"cursor": first, "user_id": str(c.actors["member"].id)}, headers=headers
        ).status_code
        == 400
    )
    assert (
        len(
            c.client.get(
                path, params={"user_id": str(c.actors["member"].id)}, headers=headers
            ).json()["items"]
        )
        == 5
    )


@pytest.mark.parametrize("token", [None, "stale-token"])
def test_csrf_failure_is_distinct_from_loss_of_admin_access(administration, token):
    c = administration
    headers = {"Cookie": c.actors["admin"].headers["Cookie"]}
    if token is not None:
        headers["X-CSRF-Token"] = token
    response = c.client.patch(
        f"/api/admin/users/{c.actors['member'].id}/role",
        json={"role": "moderator", "reason": "Причина сохраняется"},
        headers=headers,
    )
    assert response.status_code == 403 and response.json()["code"] == "csrf_invalid"
    assert c.client.get("/api/auth/me", headers=headers).json()["role"] == "admin"
    assert c.client.get("/api/admin/users", headers=headers).status_code == 200
    with c.factory() as db:
        assert db.get(User, c.actors["member"].id).role == "user"
        assert (
            list(db.scalars(select(AuditEvent).where(AuditEvent.actor_id == c.actors["admin"].id)))
            == []
        )
    assert change(c).json()["changed"] is True
