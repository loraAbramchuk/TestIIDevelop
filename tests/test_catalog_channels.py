import os
import secrets
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, event, func, select

from app.accounts import add_user_with_channel
from app.application import create_app
from app.config import Settings
from app.models import AuthSession, Channel, Subscription, User, Video, VideoGrant
from app.security import csrf_token, token_hash

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_DB_TESTS") != "1", reason="Requires PostgreSQL"),
]


@pytest.fixture
def catalog(tmp_path):
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
def test_public_discovery_and_channel_acl_including_subscribers(catalog, actor):
    c = catalog
    public = c.seed(title="Public marker")
    private = c.seed(title="Secret marker", visibility="private")
    for fields in (
        {"moderation_status": "hidden"},
        {"deleted_at": datetime.now(UTC)},
        {"processing_status": "uploading"},
        {"processing_status": "processing"},
        {"processing_status": "failed"},
    ):
        c.seed(title="Not visible marker", **fields)
    with c.factory() as db:
        db.add(
            VideoGrant(
                video_id=UUID(private),
                user_id=c.actors["allowed"].id,
                granted_by=c.actors["owner"].id,
            )
        )
        db.commit()
    channel = c.actors["owner"].channel
    assert (
        c.client.put(
            f"/api/channels/{channel}/subscription", headers=c.actors["stranger"].headers
        ).status_code
        == 200
    )
    headers = c.actors[actor].headers if actor else {}
    for query in ("", "?q=marker"):
        response = c.client.get("/api/videos" + query, headers=headers)
        assert response.status_code == 200
        assert {v["id"] for v in response.json()["items"]} == {public}
        assert "no-store" in response.headers["cache-control"]
        item = response.json()["items"][0]
        assert item["author"] == {
            "id": str(channel),
            "owner_id": str(c.actors["owner"].id),
            "name": "owner",
        }
        assert not any(key in item for key in ("source_key", "playback_key", "error_code", "email"))
    response = c.client.get(f"/api/channels/{channel}/videos", headers=headers)
    expected = {public, private} if actor in ("owner", "allowed") else {public}
    assert {v["id"] for v in response.json()["items"]} == expected
    page = c.client.get(f"/channels/{channel}", headers=headers)
    assert page.status_code == 200 and "no-store" in page.headers["cache-control"]
    assert ("Secret marker" in page.text) == (actor in ("owner", "allowed"))
    assert ("Настройки канала" in page.text) == (actor == "owner")


@pytest.mark.parametrize(
    "query,titles",
    [
        ("%", {"100% ready"}),
        ("_", {"one_two"}),
        ("/", {"dir/clip"}),
        ("\\", {"dir\\clip"}),
        ("кОт", {"КОТики"}),
        ("' OR 1=1 --", set()),
        ("  КОТ  ", {"КОТики"}),
    ],
)
def test_search_is_literal_case_insensitive_and_trimmed(catalog, query, titles):
    for title in [
        "100% ready",
        "1000 ready",
        "one_two",
        "oneAtwo",
        "dir/clip",
        "dir\\clip",
        "КОТики",
    ]:
        catalog.seed(title=title)
    response = catalog.client.get("/api/videos", params={"q": query})
    assert response.status_code == 200
    assert {v["title"] for v in response.json()["items"]} == titles


def test_public_keyset_pagination_scope_and_limits(catalog):
    stamp = datetime(2026, 1, 1, tzinfo=UTC)
    query = "Ж" * 200
    ids = {catalog.seed(title=query, created_at=stamp) for _ in range(4)}
    found = []
    cursor = None
    while True:
        params = {"q": query, "limit": 1}
        if cursor:
            params["cursor"] = cursor
        response = catalog.client.get("/api/videos", params=params)
        assert response.status_code == 200
        data = response.json()
        found.extend(item["id"] for item in data["items"])
        cursor = data["next_cursor"]
        if not cursor:
            break
        assert len(cursor) <= 1024
        assert (
            catalog.client.get(
                "/api/videos", params={"q": "different", "cursor": cursor}
            ).status_code
            == 400
        )
    assert set(found) == ids and len(found) == len(ids)
    for params, status in (
        ({"limit": 101}, 422),
        ({"limit": 0}, 422),
        ({"q": "x" * 201}, 422),
        ({"q": "\x00"}, 400),
        ({"cursor": "broken"}, 400),
    ):
        assert catalog.client.get("/api/videos", params=params).status_code == status


def test_channel_cursor_scopes_and_revocation(catalog):
    c = catalog
    channel = c.actors["owner"].channel
    for i in range(3):
        c.seed(title=f"Public {i}")
    private = c.seed(title="Secret", visibility="private")
    headers = c.actors["owner"].headers
    allowed = c.actors["allowed"]
    assert (
        c.client.put(f"/api/videos/{private}/grants/{allowed.id}", headers=headers).status_code
        == 200
    )
    response = c.client.get(f"/api/channels/{channel}/videos?limit=1", headers=allowed.headers)
    cursor = response.json()["next_cursor"]
    assert cursor
    assert (
        c.client.get(f"/api/channels/{channel}/videos", params={"cursor": cursor}).status_code
        == 400
    )
    assert (
        c.client.delete(f"/api/videos/{private}/grants/{allowed.id}", headers=headers).status_code
        == 204
    )
    response = c.client.get(
        f"/api/channels/{channel}/videos", params={"cursor": cursor}, headers=allowed.headers
    )
    assert response.status_code == 200 and all(v["id"] != private for v in response.json()["items"])


def test_channel_edit_is_owner_only_and_does_not_touch_videos(catalog):
    c = catalog
    actor = c.actors["owner"]
    url = f"/api/channels/{actor.channel}"
    video = c.seed(visibility="private")
    assert c.client.patch(url, json={"name": "New"}).status_code == 401
    assert (
        c.client.patch(
            url, json={"name": "New"}, headers={"Cookie": actor.headers["Cookie"]}
        ).status_code
        == 403
    )
    for name in ("allowed", "stranger", "moderator", "admin"):
        assert (
            c.client.patch(url, json={"name": "New"}, headers=c.actors[name].headers).status_code
            == 404
        )
    for payload in (
        {"name": " "},
        {"name": None},
        {"description": None},
        {"name": "x" * 101},
        {"description": "x" * 2001},
        {"owner_id": str(c.actors["stranger"].id)},
        {"name": "\x00"},
    ):
        assert c.client.patch(url, json=payload, headers=actor.headers).status_code == 422
    changed = c.client.patch(
        url,
        json={"name": "  Новый канал  ", "description": "<script>unsafe()</script>\nОписание"},
        headers=actor.headers,
    )
    assert changed.status_code == 200 and changed.json()["name"] == "Новый канал"
    assert c.client.patch(url, json={}, headers=actor.headers).json()["name"] == "Новый канал"
    page = c.client.get(f"/channels/{actor.channel}")
    assert "&lt;script&gt;" in page.text and "<script>unsafe()" not in page.text
    assert c.client.get(f"/api/videos/{video}").status_code == 404
    assert c.client.get("/api/channels/me", headers=actor.headers).json()["id"] == str(
        actor.channel
    )
    assert c.client.get("/api/channels/me").status_code == 401


def test_subscriptions_idempotent_private_list_and_self_guard(catalog):
    c = catalog
    owner = c.actors["owner"]
    viewer = c.actors["allowed"]
    url = f"/api/channels/{owner.channel}/subscription"
    assert c.client.put(url).status_code == 401
    assert c.client.put(url, headers={"Cookie": viewer.headers["Cookie"]}).status_code == 403
    assert c.client.put(url, headers=owner.headers).status_code == 409
    assert c.client.get("/api/subscriptions").status_code == 401
    for _ in range(2):
        data = c.client.put(url, headers=viewer.headers).json()
        assert data == {
            "channel_id": str(owner.channel),
            "is_subscribed": True,
            "subscriber_count": 1,
        }
    mine = c.client.get("/api/subscriptions", headers=viewer.headers).json()
    assert len(mine["items"]) == 1 and mine["items"][0]["is_subscribed"]
    assert (
        c.client.get("/api/subscriptions", headers=c.actors["stranger"].headers).json()["items"]
        == []
    )
    assert not c.client.get(f"/api/channels/{owner.channel}").json()["is_subscribed"]
    for _ in range(2):
        assert c.client.delete(url, headers=viewer.headers).json()["subscriber_count"] == 0
    assert c.client.get("/api/subscriptions", headers=viewer.headers).json()["items"] == []


def test_parallel_subscribe_and_unsubscribe_leave_no_duplicates(catalog):
    c = catalog
    channel = c.actors["owner"].channel
    headers = c.actors["allowed"].headers
    url = f"/api/channels/{channel}/subscription"
    for method, expected in (("PUT", 1), ("DELETE", 0)):
        with ThreadPoolExecutor(max_workers=5) as pool:
            results = list(
                pool.map(
                    lambda _, method=method: (
                        c.client.request(method, url, headers=headers).status_code
                    ),
                    range(5),
                )
            )
        assert results == [200] * 5
        with c.factory() as db:
            assert (
                db.scalar(
                    select(func.count())
                    .select_from(Subscription)
                    .where(Subscription.channel_id == channel)
                )
                == expected
            )


def test_channel_writes_share_a_per_user_limit(catalog):
    catalog.app.state.settings.video_write_limit_per_minute = 1
    headers = catalog.actors["allowed"].headers
    channel = catalog.actors["owner"].channel
    assert (
        catalog.client.put(f"/api/channels/{channel}/subscription", headers=headers).status_code
        == 200
    )
    other = catalog.actors["stranger"].channel
    response = catalog.client.put(f"/api/channels/{other}/subscription", headers=headers)
    assert response.status_code == 429 and int(response.headers["retry-after"]) > 0


def test_subscriptions_pagination_and_unknown_channel(catalog):
    c = catalog
    viewer = c.actors["allowed"]
    for name in ("owner", "stranger", "moderator"):
        assert (
            c.client.put(
                f"/api/channels/{c.actors[name].channel}/subscription", headers=viewer.headers
            ).status_code
            == 200
        )
    cursor = None
    found = []
    while True:
        params = {"limit": 1}
        if cursor:
            params["cursor"] = cursor
        data = c.client.get("/api/subscriptions", params=params, headers=viewer.headers).json()
        found.extend(v["id"] for v in data["items"])
        cursor = data["next_cursor"]
        if not cursor:
            break
        assert (
            c.client.get(
                "/api/subscriptions", params={"cursor": cursor}, headers=c.actors["owner"].headers
            ).status_code
            == 400
        )
    assert len(found) == len(set(found)) == 3
    missing = str(uuid4())
    assert c.client.get(f"/api/channels/{missing}").status_code == 404
    assert c.client.get(f"/api/channels/{missing}/videos").status_code == 404
    assert c.client.get(f"/channels/{missing}").status_code == 404
    assert (
        c.client.put(f"/api/channels/{missing}/subscription", headers=viewer.headers).status_code
        == 404
    )


def test_registration_channel_is_atomic_and_duplicates_do_not_create_channels(catalog):
    c = catalog
    with TestClient(c.app, client=(f"catalog-{uuid4().hex}", 50000)) as client:
        csrf = client.get("/api/auth/csrf").json()["csrf_token"]
        headers = {"X-CSRF-Token": csrf}
        payload = {
            "email": f"{uuid4().hex}@example.com",
            "password": "registration-test-password",
            "display_name": "Новый автор",
        }
        with c.factory() as db:
            count = db.scalar(select(func.count()).select_from(Channel))

        def reject_channel(*_):
            raise RuntimeError("simulated channel insert failure")

        event.listen(Channel, "before_insert", reject_channel)
        try:
            with pytest.raises(RuntimeError, match="simulated"):
                client.post("/api/auth/register", json=payload, headers=headers)
        finally:
            event.remove(Channel, "before_insert", reject_channel)
        with c.factory() as db:
            assert db.scalar(select(User.id).where(User.email == payload["email"])) is None
            assert db.scalar(select(func.count()).select_from(Channel)) == count
        response = client.post("/api/auth/register", json=payload, headers=headers)
        assert response.status_code == 201
        identifier = UUID(response.json()["user"]["id"])
        try:
            me = client.get("/api/channels/me")
            assert me.status_code == 200 and me.json()["name"] == payload["display_name"]
            with c.factory() as db:
                assert (
                    db.scalar(
                        select(func.count())
                        .select_from(Channel)
                        .where(Channel.owner_id == identifier)
                    )
                    == 1
                )
            with TestClient(c.app, client=(f"catalog-{uuid4().hex}", 50000)) as duplicate:
                token = duplicate.get("/api/auth/csrf").json()["csrf_token"]
                assert (
                    duplicate.post(
                        "/api/auth/register", json=payload, headers={"X-CSRF-Token": token}
                    ).status_code
                    == 409
                )
            with c.factory() as db:
                assert (
                    db.scalar(
                        select(func.count())
                        .select_from(Channel)
                        .where(Channel.owner_id == identifier)
                    )
                    == 1
                )
        finally:
            with c.factory() as db:
                db.execute(delete(User).where(User.id == identifier))
                db.commit()
