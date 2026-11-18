import json
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
from app.models import AuditEvent, AuthSession, Channel, Report, User, Video, VideoGrant
from app.security import csrf_token, token_hash

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_DB_TESTS") != "1", reason="Requires PostgreSQL"),
]


@pytest.fixture
def moderation(tmp_path):
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
        db.execute(delete(Report).where(Report.reporter_id.in_(ids)))
        db.execute(delete(AuditEvent).where(AuditEvent.actor_id.in_(ids)))
        db.execute(delete(Video).where(Video.owner_id.in_(ids)))
        db.execute(delete(User).where(User.id.in_(ids)))
        db.commit()
    app.state.engine.dispose()


def report(c, video, actor="allowed", **payload):
    return c.client.post(
        f"/api/videos/{video}/reports",
        json={"reason": "spam", "explanation": "Review request"} | payload,
        headers=c.actors[actor].headers if actor else {},
    )


def action(c, video, status, actor="moderator", reason="Обоснованное решение"):
    return c.client.post(
        f"/api/moderation/videos/{video}/visibility",
        json={"status": status, "reason": reason},
        headers=c.actors[actor].headers,
    )


@pytest.mark.parametrize("actor", [None, "owner", "allowed", "stranger", "moderator", "admin"])
@pytest.mark.parametrize(
    "state", ["public", "private", "hidden", "deleted", "uploading", "processing", "failed"]
)
def test_report_submission_uses_viewer_acl(moderation, actor, state):
    c = moderation
    fields = {}
    if state == "private":
        fields["visibility"] = "private"
    elif state == "hidden":
        fields["moderation_status"] = "hidden"
    elif state == "deleted":
        fields["deleted_at"] = datetime.now(UTC)
    elif state != "public":
        fields["processing_status"] = state
    video = c.seed(**fields)
    with c.factory() as db:
        db.add(
            VideoGrant(
                video_id=UUID(video),
                user_id=c.actors["allowed"].id,
                granted_by=c.actors["owner"].id,
            )
        )
        db.commit()
    expected = (
        401
        if actor is None
        else 201
        if state == "public" or (state == "private" and actor in ("owner", "allowed"))
        else 404
    )
    result = report(c, video, actor)
    assert result.status_code == expected, result.text
    assert "no-store" in result.headers["cache-control"]
    if expected == 201:
        assert result.json()["status"] == "open"
        assert "reporter_id" not in result.json() and "decided_by" not in result.json()


def test_staff_endpoints_and_html_are_role_protected_without_private_playback(moderation):
    c = moderation
    video = c.seed(title="Confidential title marker", visibility="private")
    with c.factory() as db:
        db.add(
            VideoGrant(
                video_id=UUID(video),
                user_id=c.actors["allowed"].id,
                granted_by=c.actors["owner"].id,
            )
        )
        db.commit()
    identifier = report(c, video).json()["id"]
    paths = [
        "/api/moderation/reports",
        f"/api/moderation/reports/{identifier}",
        f"/api/moderation/videos/{video}",
        "/api/moderation/audit",
    ]
    for actor in (None, "owner", "allowed", "stranger", "moderator", "admin"):
        headers = c.actors[actor].headers if actor else {}
        expected = 401 if actor is None else 200 if actor in ("moderator", "admin") else 403
        for path in paths:
            result = c.client.get(path, headers=headers)
            assert result.status_code == expected
            assert "Confidential title marker" not in result.text
            assert "no-store" in result.headers["cache-control"]
        html = c.client.get("/moderation", headers=headers, follow_redirects=False)
        assert html.status_code == (303 if actor is None else 200 if expected == 200 else 403)
    data = c.client.get(
        f"/api/moderation/reports/{identifier}", headers=c.actors["moderator"].headers
    ).json()
    assert data["video"]["title"] is None and data["video"]["watch_url"] is None
    assert data["video"]["can_watch"] is False
    assert (
        c.client.get(
            f"/api/videos/{video}/stream", headers=c.actors["moderator"].headers
        ).status_code
        == 404
    )
    with c.factory() as db:
        db.add(
            VideoGrant(
                video_id=UUID(video),
                user_id=c.actors["moderator"].id,
                granted_by=c.actors["owner"].id,
            )
        )
        db.commit()
    data = c.client.get(
        f"/api/moderation/videos/{video}", headers=c.actors["moderator"].headers
    ).json()
    assert data["can_watch"] and data["title"] == "Confidential title marker"


def test_parallel_duplicate_reports_and_closed_report_allow_new_case(moderation):
    c = moderation
    video = c.seed()
    with ThreadPoolExecutor(max_workers=5) as pool:
        responses = list(pool.map(lambda _: report(c, video), range(5)))
    assert sorted(r.status_code for r in responses) == [200, 200, 200, 200, 201]
    assert len({r.json()["id"] for r in responses}) == 1
    identifier = responses[0].json()["id"]
    repeat = report(c, video, reason="other", explanation="Changed input")
    assert repeat.status_code == 200 and repeat.json()["explanation"] == "Review request"
    result = c.client.post(
        f"/api/moderation/reports/{identifier}/decision",
        json={"status": "dismissed", "reason": "Нет оснований"},
        headers=c.actors["moderator"].headers,
    )
    assert result.status_code == 200
    assert report(c, video).status_code == 201
    with c.factory() as db:
        assert (
            db.scalar(
                select(func.count()).select_from(Report).where(Report.video_id == UUID(video))
            )
            == 2
        )


@pytest.mark.parametrize("status", ["resolved", "dismissed"])
def test_decision_is_atomic_idempotent_and_keeps_original_decider(moderation, status):
    c = moderation
    video = c.seed()
    identifier = report(c, video).json()["id"]
    url = f"/api/moderation/reports/{identifier}/decision"
    payload = {"status": status, "reason": "  Причина решения 🎬  "}
    first = c.client.post(url, json=payload, headers=c.actors["moderator"].headers)
    assert first.status_code == 200 and first.json()["decision_reason"] == "Причина решения 🎬"
    repeated = c.client.post(url, json=payload, headers=c.actors["admin"].headers)
    assert repeated.status_code == 200 and repeated.json()["decided_by"] == str(
        c.actors["moderator"].id
    )
    assert (
        c.client.post(
            url, json={"status": status, "reason": "Different"}, headers=c.actors["admin"].headers
        ).status_code
        == 409
    )
    with c.factory() as db:
        audit = list(db.scalars(select(AuditEvent).where(AuditEvent.object_id == UUID(identifier))))
        assert len(audit) == 1 and audit[0].action == "report." + status
        assert audit[0].actor_id == c.actors["moderator"].id
        assert db.get(Video, UUID(video)).moderation_status == "visible"


@pytest.mark.parametrize("privacy", ["public", "private"])
def test_hide_blocks_every_view_and_restore_preserves_visibility(moderation, privacy):
    c = moderation
    video = c.seed(title="Moderated marker", visibility=privacy)
    headers = c.actors["owner"].headers
    assert action(c, video, "hidden", reason="<script>unsafe()</script>").status_code == 200
    assert action(c, video, "hidden", reason="Another retry reason").status_code == 200
    for actor in (None, "owner", "allowed", "stranger", "moderator", "admin"):
        actor_headers = c.actors[actor].headers if actor else {}
        for path in (
            f"/api/videos/{video}",
            f"/api/videos/{video}/stream",
            f"/api/videos/{video}/thumbnail",
            f"/api/videos/{video}/likes",
            f"/api/videos/{video}/comments",
            f"/watch/{video}",
        ):
            assert c.client.get(path, headers=actor_headers).status_code == 404
        assert (
            c.client.head(
                f"/api/videos/{video}/stream", headers=actor_headers | {"Range": "bytes=0-1"}
            ).status_code
            == 404
        )
    assert all(v["id"] != video for v in c.client.get("/api/videos").json()["items"])
    assert (
        c.client.get(f"/api/channels/{c.actors['owner'].channel}/videos", headers=headers).json()[
            "items"
        ]
        == []
    )
    status = c.client.get(f"/api/videos/{video}/status", headers=headers).json()
    assert status["moderation_reason"] == "<script>unsafe()</script>"
    page = c.client.get(f"/studio/videos/{video}", headers=headers)
    assert (
        "&lt;script&gt;unsafe()&lt;/script&gt;" in page.text and "<script>unsafe()" not in page.text
    )
    assert (
        c.client.patch(
            f"/api/videos/{video}", json={"title": "Changed title"}, headers=headers
        ).json()["moderation_status"]
        == "hidden"
    )
    assert action(c, video, "visible", reason="Восстановлено после проверки").status_code == 200
    with c.factory() as db:
        record = db.get(Video, UUID(video))
        assert record.visibility == privacy and record.moderation_reason is None
        events = list(
            db.scalars(
                select(AuditEvent)
                .where(AuditEvent.object_id == UUID(video))
                .order_by(AuditEvent.created_at)
            )
        )
        assert [event.action for event in events] == ["video.hidden", "video.restored"]
        assert events[0].reason == "<script>unsafe()</script>"
    assert c.client.get(f"/api/videos/{video}", headers=headers).status_code == 200
    assert c.client.get(
        f"/api/videos/{video}", headers=c.actors["moderator"].headers
    ).status_code == (200 if privacy == "public" else 404)


@pytest.mark.parametrize("operation", ["visibility", "decision"])
def test_audit_failure_rolls_back_the_moderation_action(moderation, operation):

    c = moderation
    video = c.seed()
    identifier = report(c, video).json()["id"]

    def fail(*_):
        raise RuntimeError("injected audit failure")

    event.listen(AuditEvent, "before_insert", fail)
    try:
        path, payload = (
            (f"/api/moderation/videos/{video}/visibility", {"status": "hidden", "reason": "Valid"})
            if operation == "visibility"
            else (
                f"/api/moderation/reports/{identifier}/decision",
                {"status": "resolved", "reason": "Valid"},
            )
        )
        with pytest.raises(RuntimeError, match="injected audit failure"):
            c.client.post(path, json=payload, headers=c.actors["moderator"].headers)
    finally:
        event.remove(AuditEvent, "before_insert", fail)
    with c.factory() as db:
        assert db.get(Video, UUID(video)).moderation_status == "visible"
        assert db.get(Report, UUID(identifier)).status == "open"
        assert (
            db.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.actor_id == c.actors["moderator"].id)
            )
            == 0
        )


def test_mutations_require_role_csrf_and_server_owned_fields(moderation):
    c = moderation
    video = c.seed()
    identifier = report(c, video).json()["id"]
    for path, payload, role in [
        (f"/api/videos/{video}/reports", {"reason": "spam"}, "allowed"),
        (
            f"/api/moderation/reports/{identifier}/decision",
            {"status": "resolved", "reason": "Valid"},
            "moderator",
        ),
        (
            f"/api/moderation/videos/{video}/visibility",
            {"status": "hidden", "reason": "Valid"},
            "moderator",
        ),
    ]:
        assert c.client.post(path, json=payload).status_code == 401
        assert (
            c.client.post(
                path, json=payload, headers={"Cookie": c.actors[role].headers["Cookie"]}
            ).status_code
            == 403
        )
    for actor in ("owner", "allowed", "stranger"):
        assert action(c, video, "hidden", actor=actor).status_code == 403
    for reason in (" ", "x" * 2001, "A\0B", "A\ud800B", "A\udfffB", None):
        response = c.client.post(
            f"/api/moderation/videos/{video}/visibility",
            content=json.dumps({"status": "hidden", "reason": reason}),
            headers=c.actors["moderator"].headers | {"Content-Type": "application/json"},
        )
        assert response.status_code == 422
    assert (
        c.client.post(
            f"/api/moderation/videos/{video}/visibility",
            json={"status": "hidden", "reason": "Valid", "visibility": "public"},
            headers=c.actors["moderator"].headers,
        ).status_code
        == 422
    )
    for payload in (
        {"reason": "bad"},
        {"reason": "spam", "reporter_id": str(c.actors["owner"].id)},
        {"reason": "spam", "explanation": None},
        {"reason": "spam", "explanation": "x" * 2001},
        {"reason": "spam", "explanation": "\x00"},
    ):
        assert (
            c.client.post(
                f"/api/videos/{video}/reports", json=payload, headers=c.actors["allowed"].headers
            ).status_code
            == 422
        )


def wait_on_lock(db):
    import time

    from sqlalchemy import text

    deadline = time.monotonic() + 5
    while not db.scalar(
        text(
            """SELECT EXISTS(SELECT 1 FROM pg_locks w JOIN pg_locks h
            ON w.locktype=h.locktype AND w.transactionid=h.transactionid
            WHERE NOT w.granted AND h.granted AND h.pid=pg_backend_pid())"""
        )
    ):
        assert time.monotonic() < deadline, "Request did not wait on row lock"
        time.sleep(0.01)


def test_role_is_rechecked_after_waiting_for_privileged_actor_lock(moderation):
    c = moderation
    video = UUID(c.seed())
    moderator = c.actors["moderator"]
    with c.factory() as blocker, ThreadPoolExecutor(max_workers=1) as pool:
        user = blocker.scalar(select(User).where(User.id == moderator.id).with_for_update())
        future = pool.submit(action, c, video, "hidden")
        try:
            wait_on_lock(blocker)
            user.role = "user"
            blocker.commit()
            assert future.result(timeout=10).status_code == 403
        finally:
            blocker.rollback()
    with c.factory() as db:
        assert db.get(Video, video).moderation_status == "visible"


def test_waiting_report_rechecks_revoked_private_access(moderation):
    c = moderation
    video = UUID(c.seed(visibility="private"))
    with c.factory() as db:
        db.add(
            VideoGrant(
                video_id=video, user_id=c.actors["allowed"].id, granted_by=c.actors["owner"].id
            )
        )
        db.commit()
    with c.factory() as blocker, ThreadPoolExecutor(max_workers=1) as pool:
        blocker.scalar(select(Video).where(Video.id == video).with_for_update())
        future = pool.submit(report, c, video)
        try:
            wait_on_lock(blocker)
            blocker.execute(delete(VideoGrant).where(VideoGrant.video_id == video))
            blocker.commit()
            assert future.result(timeout=10).status_code == 404
        finally:
            blocker.rollback()
    with c.factory() as db:
        assert (
            db.scalar(select(func.count()).select_from(Report).where(Report.video_id == video)) == 0
        )


def test_deleted_video_cannot_be_restored_but_case_can_be_closed_and_audit_survives(moderation):
    c = moderation
    video = c.seed()
    identifier = report(c, video).json()["id"]
    assert (
        c.client.delete(f"/api/videos/{video}", headers=c.actors["owner"].headers).status_code
        == 204
    )
    assert action(c, video, "visible").status_code == 409
    result = c.client.post(
        f"/api/moderation/reports/{identifier}/decision",
        json={"status": "resolved", "reason": "Автор удалил видео"},
        headers=c.actors["admin"].headers,
    )
    assert result.status_code == 200 and result.json()["video"]["deleted"]
    with c.factory() as db:
        db.execute(delete(Report).where(Report.id == UUID(identifier)))
        db.execute(delete(Video).where(Video.id == UUID(video)))
        db.commit()
    page = c.client.get(
        "/api/moderation/audit", params={"video_id": video}, headers=c.actors["admin"].headers
    ).json()
    assert len(page["items"]) == 1 and page["items"][0]["object_id"] == identifier


def test_queue_and_audit_pagination_filters_context_and_limits(moderation):
    c = moderation
    ids = [report(c, c.seed()).json()["id"] for _ in range(4)]
    headers = c.actors["moderator"].headers
    page = c.client.get("/api/moderation/reports", params={"limit": 1}, headers=headers).json()
    cursor = page["next_cursor"]
    found = [p["id"] for p in page["items"]]
    while page["next_cursor"]:
        page = c.client.get(
            "/api/moderation/reports",
            params={"limit": 1, "cursor": page["next_cursor"]},
            headers=headers,
        ).json()
        found.extend(p["id"] for p in page["items"])
    assert len(found) == 4 and set(found) == set(ids)
    for params, actor, expected in [
        ({"cursor": cursor, "status": "all"}, "moderator", 400),
        ({"cursor": cursor}, "admin", 400),
        ({"limit": 0}, "moderator", 422),
        ({"status": "invalid"}, "moderator", 422),
    ]:
        assert (
            c.client.get(
                "/api/moderation/reports", params=params, headers=c.actors[actor].headers
            ).status_code
            == expected
        )
    for identifier in ids:
        assert (
            c.client.post(
                f"/api/moderation/reports/{identifier}/decision",
                json={"status": "dismissed", "reason": "Проверено"},
                headers=headers,
            ).status_code
            == 200
        )
    assert c.client.get("/api/moderation/reports", headers=headers).json()["items"] == []
    assert (
        len(
            c.client.get("/api/moderation/reports?status=dismissed", headers=headers).json()[
                "items"
            ]
        )
        == 4
    )
    page = c.client.get("/api/moderation/audit?limit=1", headers=headers).json()
    cursor = page["next_cursor"]
    assert cursor
    assert (
        c.client.get(
            "/api/moderation/audit", params={"cursor": cursor}, headers=c.actors["admin"].headers
        ).status_code
        == 400
    )
    assert (
        c.client.get(
            "/api/moderation/audit",
            params={"cursor": cursor, "video_id": str(uuid4())},
            headers=headers,
        ).status_code
        == 400
    )


def test_report_and_moderation_limits_are_personal(moderation):
    c = moderation
    video = c.seed()
    c.app.state.settings.report_create_limit_per_hour = 1
    assert report(c, video).status_code == 201
    result = report(c, video)
    assert result.status_code == 429 and int(result.headers["retry-after"]) > 0
    assert report(c, video, "stranger").status_code == 201
    c.app.state.settings.moderation_write_limit_per_minute = 1
    assert action(c, video, "hidden").status_code == 200
    result = action(c, video, "visible")
    assert result.status_code == 429 and int(result.headers["retry-after"]) > 0
    assert action(c, video, "visible", actor="admin").status_code == 200


def test_parallel_moderators_do_not_duplicate_actions_or_overwrite_decisions(moderation):
    c = moderation
    video = c.seed()
    identifier = report(c, video).json()["id"]
    with ThreadPoolExecutor(max_workers=5) as pool:
        responses = list(pool.map(lambda _: action(c, video, "hidden"), range(5)))
    assert all(r.status_code == 200 for r in responses)
    assert sum(r.json()["changed"] for r in responses) == 1

    def close(data):
        actor, status = data
        return c.client.post(
            f"/api/moderation/reports/{identifier}/decision",
            json={"status": status, "reason": "Проверено"},
            headers=c.actors[actor].headers,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(close, [("moderator", "resolved"), ("admin", "dismissed")]))
    assert sorted(r.status_code for r in responses) == [200, 409]
    with c.factory() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.object_id == UUID(video))
            )
            == 1
        )
        events = list(
            db.scalars(select(AuditEvent).where(AuditEvent.object_id == UUID(identifier)))
        )
        assert len(events) == 1
        assert events[0].action == "report." + db.get(Report, UUID(identifier)).status


def test_owner_changes_cannot_bypass_hidden_and_report_never_creates_grants(moderation):
    c = moderation
    video = c.seed()
    headers = c.actors["owner"].headers
    playlist = c.client.post("/api/playlists", json={"title": "My list"}, headers=headers).json()[
        "id"
    ]
    assert (
        c.client.put(f"/api/playlists/{playlist}/items/{video}", headers=headers).status_code == 204
    )
    assert report(c, video).status_code == 201
    assert action(c, video, "hidden").status_code == 200
    changed = c.client.patch(
        f"/api/videos/{video}",
        json={"visibility": "private", "description": "Edited"},
        headers=headers,
    )
    assert changed.status_code == 200 and changed.json()["moderation_status"] == "hidden"
    assert report(c, video).status_code == 404
    assert (
        c.client.get(f"/api/playlists/{playlist}/items", headers=headers).json()["visible_count"]
        == 0
    )
    assert action(c, video, "visible").status_code == 200
    assert (
        c.client.get(f"/api/playlists/{playlist}/items", headers=headers).json()["visible_count"]
        == 1
    )
    assert c.client.get(f"/api/playlists/{playlist}/items").json()["visible_count"] == 0
    assert (
        c.client.get(f"/api/videos/{video}", headers=c.actors["allowed"].headers).status_code == 404
    )
    with c.factory() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(VideoGrant)
                .where(VideoGrant.video_id == UUID(video))
            )
            == 0
        )


@pytest.mark.parametrize("initial", ["visible", "hidden"])
@pytest.mark.parametrize("desired", ["visible", "hidden"])
def test_visibility_response_distinguishes_audited_action_from_existing_state(
    moderation, initial, desired
):
    c = moderation
    video = c.seed(
        moderation_status=initial,
        moderation_reason="Прежняя причина" if initial == "hidden" else None,
    )
    result = action(c, video, desired, reason="Причина первого сотрудника")
    assert result.status_code == 200
    assert result.json()["changed"] is (initial != desired)
    repeated = action(c, video, desired, actor="admin", reason="Несохранённый текст второго")
    assert repeated.status_code == 200 and repeated.json()["changed"] is False
    assert repeated.json()["moderation_reason"] == result.json()["moderation_reason"]
    profile = c.client.get(f"/api/moderation/videos/{video}", headers=c.actors["moderator"].headers)
    assert "changed" not in profile.json()
    with c.factory() as db:
        events = list(db.scalars(select(AuditEvent).where(AuditEvent.object_id == UUID(video))))
        assert len(events) == int(initial != desired)
        if events:
            assert events[0].actor_id == c.actors["moderator"].id
            assert events[0].reason == "Причина первого сотрудника"
