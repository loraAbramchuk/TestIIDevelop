import json
import os
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, event, func, select, text

from app.accounts import add_user_with_channel
from app.application import create_app
from app.config import Settings
from app.models import AuthSession, Channel, Comment, Like, User, Video, VideoGrant
from app.security import csrf_token, token_hash

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_DB_TESTS") != "1", reason="Requires PostgreSQL"),
]


@pytest.fixture
def interactions(tmp_path):
    app = create_app(Settings(media_root=tmp_path / "media"))
    factory = app.state.session_factory
    actors = {}
    with factory() as db:
        for name, role in (
            ("owner", "user"),
            ("allowed", "user"),
            ("stranger", "user"),
            ("moderator", "moderator"),
            ("admin", "admin"),
        ):
            user = add_user_with_channel(
                db,
                email=f"{uuid4().hex}@example.com",
                display_name=name,
                password_hash="unusable-test-password",
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
                channel=db.scalar(select(Channel.id).where(Channel.owner_id == user.id)),
                headers={"Cookie": f"video_session={token}", "X-CSRF-Token": csrf_token(token)},
            )
        db.commit()
    with TestClient(app) as client:

        def seed(owner="owner", **values):
            defaults = {
                "title": "Видео автора",
                "visibility": "public",
                "processing_status": "ready",
                "duration_seconds": 2,
                "width": 320,
                "height": 180,
            }
            with factory() as db:
                video = Video(owner_id=actors[owner].id, **(defaults | values))
                db.add(video)
                db.commit()
                return str(video.id)

        yield SimpleNamespace(app=app, factory=factory, client=client, actors=actors, seed=seed)
    with factory() as db:
        ids = [a.id for a in actors.values()]
        db.execute(delete(Video).where(Video.owner_id.in_(ids)))
        db.execute(delete(User).where(User.id.in_(ids)))
        db.commit()
    app.state.engine.dispose()


@pytest.mark.parametrize("actor", [None, "owner", "allowed", "stranger", "moderator", "admin"])
@pytest.mark.parametrize(
    "state", ["public", "private", "hidden", "deleted", "uploading", "processing", "failed"]
)
def test_interaction_acl_matrix(interactions, actor, state):
    c = interactions
    fields = {}
    if state == "private":
        fields["visibility"] = "private"
    elif state == "hidden":
        fields["moderation_status"] = "hidden"
    elif state == "deleted":
        fields["deleted_at"] = datetime.now(UTC)
    elif state != "public":
        fields["processing_status"] = state
    video = UUID(c.seed(**fields))
    with c.factory() as db:
        db.add(Like(video_id=video, user_id=c.actors["owner"].id))
        db.add(
            Comment(video_id=video, user_id=c.actors["owner"].id, text="Hidden unless accessible")
        )
        db.add(
            VideoGrant(
                video_id=video, user_id=c.actors["allowed"].id, granted_by=c.actors["owner"].id
            )
        )
        db.commit()
    allowed = state == "public" or (state == "private" and actor in ("owner", "allowed"))
    headers = c.actors[actor].headers if actor else {}
    base = f"/api/videos/{video}"
    for suffix in ("likes", "comments"):
        response = c.client.get(base + "/" + suffix, headers=headers)
        assert response.status_code == (200 if allowed else 404), response.text
        assert "no-store" in response.headers["cache-control"]
        if allowed:
            data = response.json()
            assert data["like_count" if suffix == "likes" else "total_count"] == 1
        else:
            assert response.json()["code"] == "video_not_found"
            assert "Hidden unless accessible" not in response.text
    expected = 401 if actor is None else 200 if allowed else 404
    assert c.client.put(base + "/likes", headers=headers).status_code == expected
    response = c.client.post(base + "/comments", json={"text": "Valid 🎬"}, headers=headers)
    assert response.status_code == (201 if expected == 200 else expected)
    if response.status_code == 201:
        assert response.json()["can_delete"] is True
        assert "email" not in response.text and "password" not in response.text
        assert (
            c.client.delete(
                base + "/comments/" + response.json()["id"], headers=headers
            ).status_code
            == 204
        )


def test_csrf_and_authentication_for_all_writes(interactions):
    c = interactions
    video = c.seed()
    headers = c.actors["owner"].headers
    row = c.client.post(
        f"/api/videos/{video}/comments", json={"text": "Mine"}, headers=headers
    ).json()
    for method, path, data in (
        ("PUT", f"/api/videos/{video}/likes", None),
        ("DELETE", f"/api/videos/{video}/likes", None),
        ("POST", f"/api/videos/{video}/comments", {"text": "No CSRF"}),
        ("DELETE", f"/api/videos/{video}/comments/{row['id']}", None),
    ):
        assert c.client.request(method, path, json=data).status_code == 401
        assert (
            c.client.request(
                method, path, json=data, headers={"Cookie": headers["Cookie"]}
            ).status_code
            == 403
        )


def test_likes_are_idempotent_and_parallel_requests_do_not_duplicate(interactions):
    c = interactions
    video = UUID(c.seed())
    url = f"/api/videos/{video}/likes"
    headers = c.actors["allowed"].headers
    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(lambda _: c.client.put(url, headers=headers), range(5)))
    assert all(
        r.status_code == 200 and r.json()["like_count"] == 1 and r.json()["is_liked"]
        for r in results
    )
    with c.factory() as db:
        assert db.scalar(select(func.count()).select_from(Like).where(Like.video_id == video)) == 1
    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(lambda _: c.client.delete(url, headers=headers), range(5)))
    assert all(
        r.status_code == 200 and r.json()["like_count"] == 0 and not r.json()["is_liked"]
        for r in results
    )
    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(
            pool.map(lambda actor: c.client.put(url, headers=actor.headers), c.actors.values())
        )
    assert all(r.status_code == 200 for r in results)
    assert c.client.get(url).json() == {"video_id": str(video), "like_count": 5, "is_liked": False}
    assert c.client.get(url, headers=headers).json()["is_liked"] is True


def test_waiting_like_cannot_use_revoked_access(interactions):
    c = interactions
    video = UUID(c.seed(visibility="private"))
    owner, actor = c.actors["owner"], c.actors["allowed"]
    with c.factory() as db:
        db.add(VideoGrant(video_id=video, user_id=actor.id, granted_by=owner.id))
        db.commit()
    waiting = Event()

    def before_execute(_conn, _cursor, statement, *_args):
        if "FOR UPDATE" in statement:
            waiting.set()

    with c.factory() as blocker, ThreadPoolExecutor(max_workers=1) as pool:
        blocker.scalar(select(Video).where(Video.id == video).with_for_update())
        event.listen(c.app.state.engine, "before_cursor_execute", before_execute)
        try:
            future = pool.submit(c.client.put, f"/api/videos/{video}/likes", headers=actor.headers)
            assert waiting.wait(5)
            # Confirm a real PostgreSQL lock wait, not just that SQL execution started.
            deadline = time.monotonic() + 5
            while True:
                blocked = blocker.scalar(
                    text("""
                    SELECT EXISTS (
                        SELECT 1 FROM pg_locks w JOIN pg_locks h
                        ON w.locktype=h.locktype AND w.transactionid=h.transactionid
                        WHERE NOT w.granted AND h.granted AND h.pid=pg_backend_pid()
                    )
                """)
                )
                if blocked:
                    break
                assert time.monotonic() < deadline, "Request did not wait on the video lock"
                time.sleep(0.01)
            blocker.execute(delete(VideoGrant).where(VideoGrant.video_id == video))
            blocker.commit()
            assert future.result(timeout=10).status_code == 404
        finally:
            blocker.rollback()
            event.remove(c.app.state.engine, "before_cursor_execute", before_execute)
    with c.factory() as db:
        assert db.scalar(select(func.count()).select_from(Like).where(Like.video_id == video)) == 0


def test_only_comment_author_can_delete_and_revocation_covers_own_comment(interactions):
    c = interactions
    video = c.seed(visibility="private")
    other_video = c.seed()
    owner, actor = c.actors["owner"], c.actors["allowed"]
    grant = f"/api/videos/{video}/grants/{actor.id}"
    assert c.client.put(grant, headers=owner.headers).status_code == 200
    url = f"/api/videos/{video}/comments"
    created = c.client.post(
        url, json={"text": "  <script>alert(1)</script> 🎬  "}, headers=actor.headers
    )
    assert created.status_code == 201
    comment = created.json()
    assert comment["text"] == "<script>alert(1)</script> 🎬"
    for name in ("owner", "stranger", "moderator", "admin"):
        assert (
            c.client.delete(url + "/" + comment["id"], headers=c.actors[name].headers).status_code
            == 404
        )
    assert (
        c.client.delete(
            f"/api/videos/{other_video}/comments/{comment['id']}", headers=actor.headers
        ).status_code
        == 404
    )
    page = c.client.get(url, headers=owner.headers).json()
    assert page["items"][0]["can_delete"] is False
    assert page["items"][0]["author"]["id"] == str(actor.channel)
    assert c.client.delete(grant, headers=owner.headers).status_code == 204
    for endpoint in (url, f"/api/videos/{video}/likes"):
        assert c.client.get(endpoint, headers=actor.headers).status_code == 404
    assert c.client.delete(url + "/" + comment["id"], headers=actor.headers).status_code == 404
    assert c.client.post(url, json={"text": "No access"}, headers=actor.headers).status_code == 404
    assert c.client.put(grant, headers=owner.headers).status_code == 200
    assert c.client.delete(url + "/" + comment["id"], headers=actor.headers).status_code == 204
    assert c.client.get(url, headers=actor.headers).json()["total_count"] == 0


@pytest.mark.parametrize(
    "payload",
    [
        {"text": " "},
        {"text": None},
        {"text": "x" * 2001},
        {"text": "A\x00B"},
        {"text": "A\ud800B"},
        {"text": "A\udfffB"},
        {"text": 42},
        {"text": "Valid", "user_id": str(uuid4())},
        {"text": "Valid", "parent_id": str(uuid4())},
    ],
)
def test_comment_validation_and_no_partial_rows(interactions, payload):
    c = interactions
    video = UUID(c.seed())
    headers = c.actors["owner"].headers | {"Content-Type": "application/json"}
    response = c.client.post(
        f"/api/videos/{video}/comments", content=json.dumps(payload), headers=headers
    )
    assert response.status_code == 422
    assert all("input" not in item and "ctx" not in item for item in response.json()["detail"])
    with c.factory() as db:
        assert (
            db.scalar(select(func.count()).select_from(Comment).where(Comment.video_id == video))
            == 0
        )


def test_comment_keyset_ties_context_and_revocation(interactions):
    c = interactions
    video = UUID(c.seed(visibility="private"))
    owner, actor = c.actors["owner"], c.actors["allowed"]
    url = f"/api/videos/{video}/comments"
    stamp = datetime(2026, 1, 1, tzinfo=UTC)
    with c.factory() as db:
        rows = [
            Comment(video_id=video, user_id=owner.id, text=f"Comment {i}", created_at=stamp)
            for i in range(5)
        ]
        db.add_all(rows)
        db.add(VideoGrant(video_id=video, user_id=actor.id, granted_by=owner.id))
        db.commit()
        expected = {str(row.id) for row in rows}
    params, found = {"limit": 2}, []
    response = c.client.get(url, params=params, headers=actor.headers)
    cursor = response.json()["next_cursor"]
    assert cursor
    assert c.client.get(url, params={"cursor": cursor}, headers=owner.headers).status_code == 400
    while True:
        data = response.json()
        assert data["total_count"] == 5
        found.extend(item["id"] for item in data["items"])
        if not data["next_cursor"]:
            break
        response = c.client.get(
            url, params={"limit": 2, "cursor": data["next_cursor"]}, headers=actor.headers
        )
        assert response.status_code == 200
    assert len(found) == 5 and set(found) == expected
    for params in ({"limit": 0}, {"limit": 101}, {"cursor": "x" * 1025}):
        assert c.client.get(url, params=params, headers=actor.headers).status_code == 422
    assert c.client.get(url, params={"cursor": "broken"}, headers=actor.headers).status_code == 400
    another = c.seed()
    assert (
        c.client.get(
            f"/api/videos/{another}/comments", params={"cursor": cursor}, headers=actor.headers
        ).status_code
        == 400
    )
    c.client.delete(f"/api/videos/{video}/grants/{actor.id}", headers=owner.headers)
    assert c.client.get(url, params={"cursor": cursor}, headers=actor.headers).status_code == 404


def test_comment_and_interaction_rate_limits_are_per_user(interactions):
    c = interactions
    video = c.seed()
    headers = c.actors["owner"].headers
    c.app.state.settings.comment_create_limit_per_minute = 1
    url = f"/api/videos/{video}"
    assert (
        c.client.post(url + "/comments", json={"text": "One"}, headers=headers).status_code == 201
    )
    response = c.client.post(url + "/comments", json={"text": "Two"}, headers=headers)
    assert response.status_code == 429 and int(response.headers["retry-after"]) > 0
    assert c.client.put(url + "/likes", headers=headers).status_code == 200
    assert (
        c.client.post(
            url + "/comments", json={"text": "Other"}, headers=c.actors["allowed"].headers
        ).status_code
        == 201
    )
    c.app.state.settings.interaction_write_limit_per_minute = 1
    response = c.client.delete(url + "/likes", headers=headers)
    assert response.status_code == 429 and int(response.headers["retry-after"]) > 0
