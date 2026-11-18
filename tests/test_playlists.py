import json
import os
import secrets
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select

from app.accounts import add_user_with_channel
from app.application import create_app
from app.config import Settings
from app.models import (
    AuthSession,
    Channel,
    Comment,
    Like,
    Playlist,
    PlaylistItem,
    User,
    Video,
    VideoGrant,
)
from app.security import csrf_token, token_hash

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_DB_TESTS") != "1", reason="Requires PostgreSQL"),
]


@pytest.fixture
def playlists(tmp_path):
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


def create(c, actor="stranger", title="Список 🎬"):
    response = c.client.post(
        "/api/playlists", json={"title": title}, headers=c.actors[actor].headers
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def add(c, playlist, video, actor="stranger"):
    return c.client.put(f"/api/playlists/{playlist}/items/{video}", headers=c.actors[actor].headers)


def videos(c, playlist, actor="stranger", **params):
    return c.client.get(
        f"/api/playlists/{playlist}/items",
        headers=c.actors[actor].headers if actor else {},
        params=params,
    )


@pytest.mark.parametrize("actor", [None, "owner", "allowed", "stranger", "moderator", "admin"])
def test_public_playlist_filters_all_metadata_counts_and_management(playlists, actor):
    c = playlists
    playlist = create(c)
    public, private = c.seed(title="Public"), c.seed(title="Private secret", visibility="private")
    with c.factory() as db:
        for name in ("allowed", "stranger"):
            db.add(
                VideoGrant(
                    video_id=UUID(private),
                    user_id=c.actors[name].id,
                    granted_by=c.actors["owner"].id,
                )
            )
        db.commit()
    assert add(c, playlist, public).status_code == add(c, playlist, private).status_code == 204
    for index, field in enumerate(
        [
            {"moderation_status": "hidden"},
            {"deleted_at": datetime.now(UTC)},
            {"processing_status": "uploading"},
            {"processing_status": "processing"},
            {"processing_status": "failed"},
        ],
        start=2,
    ):
        hidden = c.seed(title="Inaccessible marker", **field)
        with c.factory() as db:
            db.add(PlaylistItem(playlist_id=UUID(playlist), video_id=UUID(hidden), position=index))
            db.commit()
    headers = c.actors[actor].headers if actor else {}
    expected = {public, private} if actor in ("owner", "allowed", "stranger") else {public}
    result = videos(c, playlist, actor)
    assert result.status_code == 200
    assert {v["id"] for v in result.json()["items"]} == expected
    assert result.json()["visible_count"] == len(expected)
    assert "Inaccessible marker" not in result.text and "position" not in result.text
    assert "no-store" in result.headers["cache-control"]
    profile = c.client.get(f"/api/playlists/{playlist}", headers=headers).json()
    assert profile["visible_count"] == len(expected) and profile["can_manage"] == (
        actor == "stranger"
    )
    channel_url = f"/api/channels/{c.actors['stranger'].channel}/playlists"
    assert c.client.get(channel_url, headers=headers).json()["items"][0]["visible_count"] == len(
        expected
    )
    page = c.client.get(f"/playlists/{playlist}", headers=headers)
    assert page.status_code == 200 and "Inaccessible marker" not in page.text
    if private not in expected:
        assert "Private secret" not in page.text
    if actor != "stranger":
        expected_status = 401 if actor is None else 404
        assert (
            c.client.patch(
                f"/api/playlists/{playlist}", json={"title": "No"}, headers=headers
            ).status_code
            == expected_status
        )
        assert (
            c.client.put(f"/api/playlists/{playlist}/items/{public}", headers=headers).status_code
            == expected_status
        )
        assert (
            c.client.delete(f"/api/playlists/{playlist}", headers=headers).status_code
            == expected_status
        )


def test_playlist_never_grants_access_and_owner_can_remove_revoked_member(playlists):
    c = playlists
    playlist, private = create(c), c.seed(visibility="private")
    assert add(c, playlist, private).status_code == 404
    grant = f"/api/videos/{private}/grants/{c.actors['stranger'].id}"
    assert c.client.put(grant, headers=c.actors["owner"].headers).status_code == 200
    assert add(c, playlist, private).status_code == 204
    assert videos(c, playlist).json()["visible_count"] == 1
    assert c.client.get(f"/api/videos/{private}/stream").status_code == 404
    assert c.client.delete(grant, headers=c.actors["owner"].headers).status_code == 204
    assert videos(c, playlist).json()["items"] == []
    assert add(c, playlist, private).status_code == 404
    base = f"/api/playlists/{playlist}/items/{private}"
    assert (
        c.client.put(base + "/position", json={}, headers=c.actors["stranger"].headers).status_code
        == 404
    )
    for _ in range(2):
        assert c.client.delete(base, headers=c.actors["stranger"].headers).status_code == 204
    with c.factory() as db:
        assert db.get(PlaylistItem, (UUID(playlist), UUID(private))) is None
        assert db.get(Video, UUID(private)).visibility == "private"


def test_atomic_reorder_idempotence_and_deletion_preserves_videos(playlists):
    c = playlists
    playlist = create(c)
    a, b, d = [c.seed(title=str(i)) for i in range(3)]
    for video in (a, b, d, a):
        assert add(c, playlist, video).status_code == 204
    headers = c.actors["stranger"].headers

    def move(video, before):
        return c.client.put(
            f"/api/playlists/{playlist}/items/{video}/position",
            json={"before_video_id": before},
            headers=headers,
        )

    assert move(d, a).status_code == 204
    assert [v["id"] for v in videos(c, playlist).json()["items"]] == [d, a, b]
    assert move(a, None).status_code == 204
    assert [v["id"] for v in videos(c, playlist).json()["items"]] == [d, b, a]
    assert move(a, a).status_code == 409
    assert move(a, str(uuid4())).status_code == 404
    assert (
        c.client.delete(f"/api/playlists/{playlist}/items/{b}", headers=headers).status_code == 204
    )
    with c.factory() as db:
        members = list(
            db.scalars(
                select(PlaylistItem)
                .where(PlaylistItem.playlist_id == UUID(playlist))
                .order_by(PlaylistItem.position)
            )
        )
        assert [m.position for m in members] == [0, 1]
        db.add(Like(video_id=UUID(a), user_id=c.actors["owner"].id))
        db.add(Comment(video_id=UUID(a), user_id=c.actors["owner"].id, text="Keep"))
        db.commit()
    assert c.client.delete(f"/api/playlists/{playlist}", headers=headers).status_code == 204
    assert c.client.get(f"/api/playlists/{playlist}").status_code == 404
    with c.factory() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(PlaylistItem)
                .where(PlaylistItem.playlist_id == UUID(playlist))
            )
            == 0
        )
        assert all(db.get(Video, UUID(video)).deleted_at is None for video in (a, b, d))
        assert db.get(Like, (UUID(a), c.actors["owner"].id)) is not None
        assert db.scalar(select(Comment.text).where(Comment.video_id == UUID(a))) == "Keep"


def test_parallel_adds_moves_removes_leave_consistent_unique_order(playlists):
    c = playlists
    playlist = create(c)
    ids = [c.seed(title=f"Video {i}") for i in range(8)]
    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(lambda _: add(c, playlist, ids[0]), range(5)))
    assert all(r.status_code == 204 for r in results)
    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(lambda video: add(c, playlist, video), ids[1:]))
    assert all(r.status_code == 204 for r in results)
    headers = c.actors["stranger"].headers

    def mutate(index):
        if index % 2:
            return c.client.delete(f"/api/playlists/{playlist}/items/{ids[-1]}", headers=headers)
        return c.client.put(
            f"/api/playlists/{playlist}/items/{ids[(index // 2) % 3]}/position",
            json={"before_video_id": ids[(index // 2 + 3) % 7]},
            headers=headers,
        )

    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(mutate, range(12)))
    assert all(r.status_code == 204 for r in results), [r.text for r in results]
    with c.factory() as db:
        rows = list(
            db.scalars(
                select(PlaylistItem)
                .where(PlaylistItem.playlist_id == UUID(playlist))
                .order_by(PlaylistItem.position)
            )
        )
        assert len(rows) == 7 and {str(r.video_id) for r in rows} == set(ids[:-1])
        assert [r.position for r in rows] == list(range(7))


def test_cursor_hides_positions_binds_context_and_detects_reorder(playlists):
    import base64

    c = playlists
    playlist = create(c)
    ids = [c.seed(title=str(i)) for i in range(4)]
    for video in ids:
        assert add(c, playlist, video).status_code == 204
    page = videos(c, playlist, None, limit=1).json()
    cursor = page["next_cursor"]
    parts = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
    assert all(isinstance(p, str) for p in parts) and parts[-1] == ids[0]
    with c.factory() as db:
        assert str(db.get(Playlist, UUID(playlist)).revision) not in parts
    forged = parts.copy()
    forged[-1] = ids[1]
    value = base64.urlsafe_b64encode(json.dumps(forged).encode()).decode().rstrip("=")
    assert videos(c, playlist, None, cursor=value).status_code == 400
    forged[-1] = str(uuid4())
    value = base64.urlsafe_b64encode(json.dumps(forged).encode()).decode().rstrip("=")
    assert videos(c, playlist, None, cursor=value).status_code == 400
    assert videos(c, playlist, "owner", cursor=cursor).status_code == 400
    found = [page["items"][0]["id"]]
    while page["next_cursor"]:
        response = videos(c, playlist, None, limit=1, cursor=page["next_cursor"])
        assert response.status_code == 200
        page = response.json()
        found.extend(v["id"] for v in page["items"])
    assert found == ids
    another = create(c)
    assert videos(c, another, None, cursor=cursor).status_code == 400
    assert videos(c, playlist, None, cursor="broken").status_code == 400
    for params in ({"limit": 0}, {"limit": 101}, {"cursor": "x" * 1025}):
        assert videos(c, playlist, None, **params).status_code == 422
    assert (
        c.client.patch(
            f"/api/playlists/{playlist}",
            json={"title": "Renamed"},
            headers=c.actors["stranger"].headers,
        ).status_code
        == 200
    )
    assert add(c, playlist, ids[0]).status_code == 204
    assert videos(c, playlist, None, cursor=cursor).status_code == 200
    assert (
        c.client.put(
            f"/api/playlists/{playlist}/items/{ids[-1]}/position",
            json={"before_video_id": ids[0]},
            headers=c.actors["stranger"].headers,
        ).status_code
        == 204
    )
    assert videos(c, playlist, None, cursor=cursor).status_code == 409
    assert c.client.get(f"/playlists/{playlist}", params={"cursor": cursor}).status_code == 409


@pytest.mark.parametrize(
    "payload",
    [
        {"title": " "},
        {"title": None},
        {"title": "x" * 201},
        {"title": "A\0B"},
        {"title": "A\ud800B"},
        {"title": "A\udfffB"},
        {"title": "Valid", "owner_id": str(uuid4())},
        {"title": "Valid", "visibility": "private"},
    ],
)
def test_title_validation_is_422_and_does_not_create_rows(playlists, payload):
    c = playlists
    with c.factory() as db:
        count = db.scalar(select(func.count()).select_from(Playlist))
    response = c.client.post(
        "/api/playlists",
        content=json.dumps(payload),
        headers=c.actors["stranger"].headers | {"Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert all("input" not in item and "ctx" not in item for item in response.json()["detail"])
    with c.factory() as db:
        assert db.scalar(select(func.count()).select_from(Playlist)) == count


def test_all_mutations_need_csrf_and_membership_body_is_validated(playlists):
    c = playlists
    playlist, video = create(c), c.seed()
    base = f"/api/playlists/{playlist}"
    for method, path, body in [
        ("POST", "/api/playlists", {"title": "New"}),
        ("PATCH", base, {"title": "New"}),
        ("DELETE", base, None),
        ("PUT", base + f"/items/{video}", None),
        ("DELETE", base + f"/items/{video}", None),
        ("PUT", base + f"/items/{video}/position", {}),
    ]:
        assert c.client.request(method, path, json=body).status_code == 401
        assert (
            c.client.request(
                method, path, json=body, headers={"Cookie": c.actors["stranger"].headers["Cookie"]}
            ).status_code
            == 403
        )
    assert add(c, playlist, video).status_code == 204
    for body in ({"position": 1}, {"before_video_id": "broken"}, {"before_video_id": 1}):
        assert (
            c.client.put(
                base + f"/items/{video}/position", json=body, headers=c.actors["stranger"].headers
            ).status_code
            == 422
        )
    assert c.client.get("/my-playlists", follow_redirects=False).status_code == 303
    assert c.client.get("/playlists/invalid").status_code == 422
    assert c.client.get(f"/playlists/{uuid4()}").status_code == 404


def test_limits_and_capacity_are_atomic_and_per_user(playlists):
    c = playlists
    playlist = create(c)
    c.app.state.settings.playlist_max_items = 2
    ids = [c.seed() for _ in range(5)]
    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(lambda video: add(c, playlist, video), ids))
    assert sorted(r.status_code for r in results) == [204, 204, 409, 409, 409]
    page = videos(c, playlist).json()
    assert page["visible_count"] == 2
    assert add(c, playlist, page["items"][0]["id"]).status_code == 204
    c.app.state.settings.playlist_create_limit_per_hour = 1
    response = c.client.post(
        "/api/playlists", json={"title": "No"}, headers=c.actors["stranger"].headers
    )
    assert response.status_code == 429 and int(response.headers["retry-after"]) > 0
    assert (
        c.client.post(
            "/api/playlists", json={"title": "Other"}, headers=c.actors["owner"].headers
        ).status_code
        == 201
    )
    c.app.state.settings.playlist_write_limit_per_minute = 1
    response = add(c, playlist, ids[0])
    assert response.status_code == 429 and int(response.headers["retry-after"]) > 0


def test_playlist_list_pagination_escaped_html_and_viewer_scope(playlists):
    c = playlists
    names = ["<script>window.playlistXss=1</script>", "Второй", "Третий"]
    ids = {create(c, title=name) for name in names}
    channel = c.actors["stranger"].channel
    url = f"/api/channels/{channel}/playlists"
    page = c.client.get(url, params={"limit": 1}).json()
    cursor = page["next_cursor"]
    found = [p["id"] for p in page["items"]]
    while page["next_cursor"]:
        page = c.client.get(url, params={"limit": 1, "cursor": page["next_cursor"]}).json()
        found.extend(p["id"] for p in page["items"])
    assert len(found) == 3 and set(found) == ids
    assert (
        c.client.get(url, params={"cursor": cursor}, headers=c.actors["owner"].headers).status_code
        == 400
    )
    assert c.client.get("/api/playlists").status_code == 401
    assert {
        p["id"]
        for p in c.client.get("/api/playlists", headers=c.actors["stranger"].headers).json()[
            "items"
        ]
    } == ids
    channel_page = c.client.get(f"/channels/{channel}")
    assert channel_page.status_code == 200
    assert (
        "&lt;script&gt;" in channel_page.text
        and "<script>window.playlistXss" not in channel_page.text
    )


def test_visibility_changes_are_rechecked_on_pages_without_changing_membership(playlists):
    c = playlists
    playlist = create(c)
    ids = [c.seed(title=f"Visible {i}") for i in range(4)]
    for video in ids:
        assert add(c, playlist, video).status_code == 204
    first = videos(c, playlist, None, limit=1).json()
    cursor = first["next_cursor"]
    owner = c.actors["owner"].headers
    # Revoke the cursor anchor and hide/delete later members without editing the playlist.
    assert (
        c.client.patch(
            f"/api/videos/{ids[0]}", json={"visibility": "private"}, headers=owner
        ).status_code
        == 200
    )
    with c.factory() as db:
        db.get(Video, UUID(ids[1])).moderation_status = "hidden"
        db.get(Video, UUID(ids[2])).deleted_at = datetime.now(UTC)
        db.commit()
    result = videos(c, playlist, None, cursor=cursor)
    assert result.status_code == 200
    assert [v["id"] for v in result.json()["items"]] == [ids[3]]
    assert result.json()["visible_count"] == 1
    profile = c.client.get(f"/api/playlists/{playlist}").json()
    assert profile["visible_count"] == 1
    with c.factory() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(PlaylistItem)
                .where(PlaylistItem.playlist_id == UUID(playlist))
            )
            == 4
        )


def test_waiting_add_cannot_keep_revoked_video_access(playlists):
    import time

    from sqlalchemy import text

    c = playlists
    playlist, video = create(c), UUID(c.seed(visibility="private"))
    owner, actor = c.actors["owner"], c.actors["stranger"]
    with c.factory() as db:
        db.add(VideoGrant(video_id=video, user_id=actor.id, granted_by=owner.id))
        db.commit()
    with c.factory() as blocker, ThreadPoolExecutor(max_workers=1) as pool:
        blocker.scalar(select(Video).where(Video.id == video).with_for_update())
        future = pool.submit(add, c, playlist, video)
        try:
            deadline = time.monotonic() + 5
            while True:
                blocked = blocker.scalar(
                    text("""
                    SELECT EXISTS (SELECT 1 FROM pg_locks w JOIN pg_locks h
                    ON w.locktype=h.locktype AND w.transactionid=h.transactionid
                    WHERE NOT w.granted AND h.granted AND h.pid=pg_backend_pid())
                """)
                )
                if blocked:
                    break
                assert time.monotonic() < deadline, "Request did not wait on video lock"
                time.sleep(0.01)
            blocker.execute(delete(VideoGrant).where(VideoGrant.video_id == video))
            blocker.commit()
            assert future.result(timeout=10).status_code == 404
        finally:
            blocker.rollback()
    assert videos(c, playlist).json()["visible_count"] == 0
