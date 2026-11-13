import base64
import json
import os
import secrets
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.application import create_app
from app.config import Settings
from app.models import AuthRateLimit, AuthSession, User, Video, VideoGrant
from app.security import csrf_token, token_hash
from app.videos.access import viewable_by

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("RUN_DB_TESTS") != "1", reason="Set RUN_DB_TESTS=1 for PostgreSQL"
    ),
]
STAMP = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def video_app(tmp_path):
    app = create_app(Settings(media_root=tmp_path / "media"))
    with app.state.engine.connect() as connection:
        transaction = connection.begin()
        app.state.session_factory = sessionmaker(
            bind=connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        try:
            yield app
        finally:
            transaction.rollback()
    app.state.engine.dispose()


@pytest.fixture
def actors(video_app):
    result = {}
    with video_app.state.session_factory() as db:
        for name, role in {
            "owner": "user",
            "allowed": "user",
            "stranger": "user",
            "moderator": "moderator",
            "admin": "admin",
        }.items():
            user = User(
                email=f"{uuid4().hex}@example.com",
                display_name=name,
                password_hash="test-only-unusable",
                role=role,
            )
            db.add(user)
            db.flush()
            token = secrets.token_urlsafe(32)
            db.add(
                AuthSession(
                    token_hash=token_hash(token),
                    user_id=user.id,
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                )
            )
            result[name] = {"id": user.id, "token": token}
        db.commit()
    return result


@pytest.fixture
def video_client(video_app):
    with TestClient(video_app, base_url="https://testserver") as client:
        yield client


def act(client, actors, name):
    client.cookies.clear()
    if name is None:
        return {}
    client.cookies.set("video_session", actors[name]["token"])
    response = client.get("/api/auth/csrf")
    assert response.status_code == 200
    return {"X-CSRF-Token": response.json()["csrf_token"]}


@pytest.fixture
def seed_video(video_app, actors):
    def seed(*, owner="owner", **values):
        defaults = {
            "owner_id": actors[owner]["id"],
            "title": "Private sample",
            "visibility": "private",
            "processing_status": "ready",
            "duration_seconds": 12.5,
            "width": 640,
            "height": 360,
            "created_at": STAMP,
        }
        with video_app.state.session_factory() as db:
            video = Video(**(defaults | values))
            db.add(video)
            db.commit()
            return video.id

    return seed


def test_create_defaults_and_owner_status(video_client, video_app, actors):
    headers = act(video_client, actors, "owner")
    response = video_client.post("/api/videos", json={"title": "  My video  "}, headers=headers)
    assert response.status_code == 201, response.text
    data = response.json()
    assert data["owner_id"] == str(actors["owner"]["id"])
    assert data["title"] == "My video" and data["visibility"] == "private"
    assert data["processing_status"] == "uploading" and data["moderation_status"] == "visible"
    assert data["created_at"] and data["updated_at"]
    assert "stream_url" not in data and "storage_key" not in data
    assert video_client.get(f"/api/videos/{data['id']}/status").json() == data
    assert video_client.get(f"/api/videos/{data['id']}").status_code == 404
    with video_app.state.session_factory() as db:
        video = db.get(Video, UUID(data["id"]))
        assert video.duration_seconds is None and video.deleted_at is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("owner_id", str(uuid4())),
        ("processing_status", "ready"),
        ("moderation_status", "visible"),
        ("deleted_at", None),
        ("error_code", "forged"),
        ("storage_key", "secret"),
    ],
)
def test_cannot_forge_protected_fields(video_client, actors, seed_video, field, value):
    video_id = seed_video()
    headers = act(video_client, actors, "owner")
    assert (
        video_client.post(
            "/api/videos", json={"title": "Forged", field: value}, headers=headers
        ).status_code
        == 422
    )
    assert (
        video_client.patch(
            f"/api/videos/{video_id}", json={field: value}, headers=headers
        ).status_code
        == 422
    )


@pytest.mark.parametrize("role", ["user", "moderator", "admin"])
def test_any_role_can_create_own_video_without_extra_access(video_client, video_app, actors, role):
    with video_app.state.session_factory() as db:
        db.get(User, actors["owner"]["id"]).role = role
        db.commit()
    response = video_client.post(
        "/api/videos", json={"title": "Own"}, headers=act(video_client, actors, "owner")
    )
    assert response.status_code == 201
    assert response.json()["owner_id"] == str(actors["owner"]["id"])


@pytest.mark.parametrize(
    "visibility,status,moderation,deleted,allowed_names",
    [
        ("private", "ready", "visible", False, {"owner", "allowed"}),
        (
            "public",
            "ready",
            "visible",
            False,
            {None, "owner", "allowed", "stranger", "moderator", "admin"},
        ),
        ("public", "ready", "hidden", False, set()),
        ("private", "uploading", "visible", False, set()),
        ("public", "processing", "visible", False, set()),
        ("public", "failed", "visible", False, set()),
        ("public", "ready", "visible", True, set()),
    ],
)
def test_view_policy_matrix_and_sql_filter(
    video_client,
    video_app,
    actors,
    seed_video,
    visibility,
    status,
    moderation,
    deleted,
    allowed_names,
):
    video_id = seed_video(
        visibility=visibility,
        processing_status=status,
        moderation_status=moderation,
        deleted_at=STAMP if deleted else None,
    )
    with video_app.state.session_factory() as db:
        db.add(
            VideoGrant(
                video_id=video_id, user_id=actors["allowed"]["id"], granted_by=actors["owner"]["id"]
            )
        )
        db.commit()
    for actor in (None, "owner", "allowed", "stranger", "moderator", "admin"):
        act(video_client, actors, actor)
        response = video_client.get(f"/api/videos/{video_id}")
        expected = 200 if actor in allowed_names else 404
        assert response.status_code == expected, (
            actor,
            visibility,
            status,
            moderation,
            response.text,
        )
        viewer_id = actors[actor]["id"] if actor else None
        with video_app.state.session_factory() as db:
            matching = set(db.scalars(select(Video.id).where(viewable_by(viewer_id))))
        assert (video_id in matching) == (expected == 200)
        assert response.headers["cache-control"] == "private, no-store"
        assert "Cookie" in response.headers["vary"]
        if expected == 200:
            assert response.json()["stream_url"] == f"/api/videos/{video_id}/stream"
            assert (
                "error_code" not in response.json() and "moderation_reason" not in response.json()
            )
        else:
            assert response.json() == {"detail": "Видео не найдено", "code": "video_not_found"}


@pytest.mark.parametrize("actor", ["allowed", "stranger", "moderator", "admin"])
def test_nonowner_cannot_manage_even_public_video(video_client, actors, seed_video, actor):
    video_id = seed_video(visibility="public")
    headers = act(video_client, actors, actor)
    base = f"/api/videos/{video_id}"
    assert video_client.get(base).status_code == 200
    assert video_client.get(base + "/status").status_code == 404
    assert video_client.get(base + "/grants").status_code == 404
    assert video_client.get(f"/studio/videos/{video_id}").status_code == 404
    assert video_client.patch(base, json={"title": "Hijack"}, headers=headers).status_code == 404
    assert (
        video_client.put(base + f"/grants/{actors[actor]['id']}", headers=headers).status_code
        == 404
    )
    assert (
        video_client.delete(
            base + f"/grants/{actors['allowed']['id']}", headers=headers
        ).status_code
        == 404
    )


def test_patch_does_not_clear_hidden_failed_state(video_client, video_app, actors, seed_video):
    video_id = seed_video(
        processing_status="failed",
        moderation_status="hidden",
        moderation_reason="Reason",
        error_code="invalid_format",
    )
    headers = act(video_client, actors, "owner")
    base = f"/api/videos/{video_id}"
    original = video_client.get(base + "/status").json()
    response = video_client.patch(
        base,
        json={"title": " Renamed ", "description": "Changed", "visibility": "public"},
        headers=headers,
    )
    assert response.status_code == 200
    result = response.json()
    assert result["title"] == "Renamed" and result["description"] == "Changed"
    for key in (
        "processing_status",
        "moderation_status",
        "moderation_reason",
        "error_code",
        "owner_id",
        "created_at",
    ):
        assert result[key] == original[key]
    assert datetime.fromisoformat(result["updated_at"]) >= datetime.fromisoformat(
        original["updated_at"]
    )
    assert video_client.get(base).status_code == 404
    assert video_client.patch(base, json={}, headers=headers).json() == result


def test_grant_idempotence_and_immediate_revoke(video_client, video_app, actors, seed_video):
    video_id = seed_video()
    base = f"/api/videos/{video_id}"
    headers = act(video_client, actors, "owner")
    url = base + f"/grants/{actors['allowed']['id']}"
    first = video_client.put(url, headers=headers)
    assert first.status_code == 200
    assert video_client.put(url, headers=headers).json() == first.json()
    assert video_client.get(base + "/grants").json()["items"] == [first.json()]
    with video_app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(VideoGrant)) == 1
    act(video_client, actors, "allowed")
    assert video_client.get(base).status_code == 200
    assert video_client.get(base + "/grants").status_code == 404
    act(video_client, actors, "stranger")
    assert video_client.get(base).status_code == 404
    headers = act(video_client, actors, "owner")
    assert video_client.delete(url, headers=headers).status_code == 204
    assert video_client.delete(url, headers=headers).status_code == 204
    act(video_client, actors, "allowed")
    assert video_client.get(base).status_code == 404


def test_grant_prepared_before_ready_and_public_to_private(
    video_client, video_app, actors, seed_video
):
    video_id = seed_video(processing_status="uploading", visibility="public")
    headers = act(video_client, actors, "owner")
    base = f"/api/videos/{video_id}"
    assert (
        video_client.put(base + f"/grants/{actors['allowed']['id']}", headers=headers).status_code
        == 200
    )
    act(video_client, actors, "allowed")
    assert video_client.get(base).status_code == 404
    # Fixture-only transition: the API must never allow a client to set ready.
    with video_app.state.session_factory() as db:
        db.get(Video, video_id).processing_status = "ready"
        db.commit()
    act(video_client, actors, "stranger")
    assert video_client.get(base).status_code == 200
    headers = act(video_client, actors, "owner")
    assert (
        video_client.patch(base, json={"visibility": "private"}, headers=headers).status_code == 200
    )
    act(video_client, actors, "stranger")
    assert video_client.get(base).status_code == 404
    act(video_client, actors, "allowed")
    assert video_client.get(base).status_code == 200


@pytest.mark.parametrize("actor", ["moderator", "admin"])
def test_privileged_roles_need_and_can_receive_explicit_grant(
    video_client, actors, seed_video, actor
):
    video_id = seed_video()
    base = f"/api/videos/{video_id}"
    act(video_client, actors, actor)
    assert video_client.get(base).status_code == 404
    headers = act(video_client, actors, "owner")
    assert (
        video_client.put(base + f"/grants/{actors[actor]['id']}", headers=headers).status_code
        == 200
    )
    act(video_client, actors, actor)
    assert video_client.get(base).status_code == 200


def test_deleted_video_is_absent_from_owner_management(video_client, actors, seed_video):
    video_id = seed_video(deleted_at=STAMP)
    headers = act(video_client, actors, "owner")
    base = f"/api/videos/{video_id}"
    for path in (base, base + "/status", base + "/grants", f"/studio/videos/{video_id}"):
        assert video_client.get(path).status_code == 404
    assert video_client.patch(base, json={"title": "Restore"}, headers=headers).status_code == 404
    assert (
        video_client.put(base + f"/grants/{actors['allowed']['id']}", headers=headers).status_code
        == 404
    )
    assert video_client.get("/api/studio/videos").json()["items"] == []


def test_owner_grant_rejected_and_recipient_checked_after_ownership(
    video_client, actors, seed_video
):
    video_id = seed_video()
    headers = act(video_client, actors, "owner")
    base = f"/api/videos/{video_id}/grants"
    assert video_client.put(base + f"/{actors['owner']['id']}", headers=headers).status_code == 409
    assert (
        video_client.put(base + f"/{uuid4()}", headers=headers).json()["code"]
        == "recipient_not_found"
    )
    headers = act(video_client, actors, "stranger")
    assert (
        video_client.put(base + f"/{uuid4()}", headers=headers).json()["code"] == "video_not_found"
    )


def test_owner_pages_pagination_and_cursor_scoping(video_client, actors, seed_video):
    own_ids = {str(seed_video(title=f"Own {i}")) for i in range(3)}
    for i in range(2):
        seed_video(owner="stranger", title=f"Other {i}")
    seed_video(deleted_at=STAMP)
    act(video_client, actors, "owner")
    first = video_client.get("/api/studio/videos?limit=1").json()
    assert first["next_cursor"]
    seen = {first["items"][0]["id"]}
    cursor = first["next_cursor"]
    while cursor:
        page = video_client.get("/api/studio/videos", params={"limit": 1, "cursor": cursor}).json()
        assert page["items"][0]["id"] not in seen
        seen.add(page["items"][0]["id"])
        cursor = page["next_cursor"]
    assert seen == own_ids
    response = video_client.get("/studio")
    assert (
        response.status_code == 200 and "Own 0" in response.text and "Other 0" not in response.text
    )
    act(video_client, actors, "stranger")
    assert (
        video_client.get("/api/studio/videos", params={"cursor": first["next_cursor"]}).status_code
        == 400
    )
    act(video_client, actors, None)
    assert video_client.get("/studio", follow_redirects=False).status_code == 303


def test_grants_cursor_ties_and_malformed_input(video_client, actors, seed_video):
    video_id, other_video = seed_video(), seed_video()
    headers = act(video_client, actors, "owner")
    expected = {str(actors[name]["id"]) for name in ("allowed", "stranger", "moderator")}
    for name in ("allowed", "stranger", "moderator"):
        assert (
            video_client.put(
                f"/api/videos/{video_id}/grants/{actors[name]['id']}", headers=headers
            ).status_code
            == 200
        )
    base = f"/api/videos/{video_id}/grants"
    first = video_client.get(base, params={"limit": 1}).json()
    seen = {first["items"][0]["user_id"]}
    cursor = first["next_cursor"]
    while cursor:
        result = video_client.get(base, params={"limit": 1, "cursor": cursor}).json()
        assert result["items"][0]["user_id"] not in seen
        seen.add(result["items"][0]["user_id"])
        cursor = result["next_cursor"]
    assert seen == expected
    assert (
        video_client.get(
            f"/api/videos/{other_video}/grants", params={"cursor": first["next_cursor"]}
        ).status_code
        == 400
    )
    malformed = base64.urlsafe_b64encode(
        json.dumps([f"grants:{video_id}", None, 123]).encode()
    ).decode()
    for cursor in ("not!base64", "____", malformed):
        assert video_client.get(base, params={"cursor": cursor}).status_code == 400
    act(video_client, actors, "stranger")
    assert video_client.get(base, params={"cursor": malformed}).status_code == 404
    act(video_client, actors, "owner")
    assert video_client.get(base, params={"limit": 0}).status_code == 422
    assert video_client.get(base, params={"limit": 101}).status_code == 422


def test_authentication_csrf_and_unimplemented_media(video_client, actors, seed_video):
    video_id = seed_video()
    actions = [
        ("post", "/api/videos", {"title": "New"}),
        ("patch", f"/api/videos/{video_id}", {"title": "Changed"}),
        ("put", f"/api/videos/{video_id}/grants/{actors['allowed']['id']}", None),
        ("delete", f"/api/videos/{video_id}/grants/{actors['allowed']['id']}", None),
    ]
    for method, url, body in actions:
        act(video_client, actors, None)
        assert video_client.request(method, url, json=body).status_code == 401
        act(video_client, actors, "owner")
        for headers in ({}, {"X-CSRF-Token": "wrong"}):
            assert video_client.request(method, url, json=body, headers=headers).status_code == 403
    for suffix in ("stream", "thumbnail", "content"):
        response = video_client.get(f"/api/videos/{video_id}/{suffix}")
        assert response.status_code == 404
    assert (
        video_client.delete(
            f"/api/videos/{video_id}", headers=act(video_client, actors, "owner")
        ).status_code
        == 405
    )


def test_metadata_rate_limits(video_client, video_app, actors, seed_video):
    video_app.state.settings.video_create_limit_per_hour = 2
    video_app.state.settings.video_write_limit_per_minute = 1
    headers = act(video_client, actors, "owner")
    for _ in range(2):
        assert (
            video_client.post("/api/videos", json={"title": "New"}, headers=headers).status_code
            == 201
        )
    assert (
        video_client.post("/api/videos", json={"title": "Blocked"}, headers=headers).status_code
        == 429
    )
    video_id = seed_video()
    assert (
        video_client.patch(
            f"/api/videos/{video_id}", json={"title": "Updated"}, headers=headers
        ).status_code
        == 200
    )
    assert (
        video_client.patch(
            f"/api/videos/{video_id}", json={"title": "Blocked"}, headers=headers
        ).status_code
        == 429
    )


@pytest.mark.parametrize(
    "invalid",
    [
        {"visibility": "unlisted"},
        {"processing_status": "invalid"},
        {"moderation_status": "invalid"},
        {"title": " "},
        {"description": "x" * 5001},
        {"processing_status": "ready"},
        {"duration_seconds": float("inf")},
        {"duration_seconds": float("nan")},
        {"width": 0},
        {"height": -1},
    ],
)
def test_database_constraints(video_app, actors, invalid):
    with video_app.state.session_factory() as db:
        with pytest.raises(IntegrityError), db.begin_nested():
            db.add(
                Video(owner_id=actors["owner"]["id"], title="Valid", **invalid)
                if "title" not in invalid
                else Video(owner_id=actors["owner"]["id"], **invalid)
            )
            db.flush()


def test_parallel_grants_and_revokes_are_idempotent(tmp_path):
    app = create_app(Settings(media_root=tmp_path / "parallel-media"))
    owner_id, recipient_id, video_id = uuid4(), uuid4(), uuid4()
    token = secrets.token_urlsafe(32)
    try:
        with app.state.session_factory() as db:
            db.add_all(
                [
                    User(
                        id=identifier,
                        email=f"{identifier}@example.com",
                        display_name="Concurrent",
                        password_hash="test-only",
                    )
                    for identifier in (owner_id, recipient_id)
                ]
            )
            db.flush()
            db.add(Video(id=video_id, owner_id=owner_id, title="Concurrent"))
            db.add(
                AuthSession(
                    token_hash=token_hash(token),
                    user_id=owner_id,
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                )
            )
            db.commit()
        headers = {"Cookie": f"video_session={token}", "X-CSRF-Token": csrf_token(token)}
        url = f"/api/videos/{video_id}/grants/{recipient_id}"
        with TestClient(app, base_url="https://testserver") as client:
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(lambda _: client.put(url, headers=headers), range(8)))
            assert all(response.status_code == 200 for response in results)
            assert len({response.json()["created_at"] for response in results}) == 1
            with app.state.session_factory() as db:
                assert (
                    db.scalar(
                        select(func.count())
                        .select_from(VideoGrant)
                        .where(VideoGrant.video_id == video_id)
                    )
                    == 1
                )
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(lambda _: client.delete(url, headers=headers), range(8)))
            assert all(response.status_code == 204 for response in results)
            with app.state.session_factory() as db:
                assert (
                    db.scalar(
                        select(func.count())
                        .select_from(VideoGrant)
                        .where(VideoGrant.video_id == video_id)
                    )
                    == 0
                )
    finally:
        with app.state.session_factory() as db:
            db.execute(delete(Video).where(Video.id == video_id))
            db.execute(delete(User).where(User.id.in_([owner_id, recipient_id])))
            db.execute(
                delete(AuthRateLimit).where(
                    AuthRateLimit.key == token_hash(f"videos:write:{owner_id}")
                )
            )
            db.commit()
        app.state.engine.dispose()
