"""Application-side ACL/delegation; actual HTTP bytes/Range are tested through Nginx."""

import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from test_videos import act
from test_videos import actors as actors
from test_videos import seed_video as seed_video
from test_videos import video_app as video_app
from test_videos import video_client as video_client

from app.models import Video, VideoGrant

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_DB_TESTS") != "1", reason="Requires PostgreSQL"),
]


@pytest.fixture
def media_video(video_app, seed_video):
    def create(**values):
        identifier = seed_video(**values)
        run_id = uuid4().hex
        root = video_app.state.settings.media_root / "processing" / run_id
        root.mkdir(parents=True)
        (root / "playback.mp4").write_bytes(b"test mp4 bytes")
        (root / "thumbnail.jpg").write_bytes(b"\xff\xd8test jpeg")
        with video_app.state.session_factory() as db:
            video = db.get(Video, identifier)
            video.playback_key = f"processing/{run_id}/playback.mp4"
            video.thumbnail_key = f"processing/{run_id}/thumbnail.jpg"
            video.playback_bytes = (root / "playback.mp4").stat().st_size
            video.thumbnail_bytes = (root / "thumbnail.jpg").stat().st_size
            db.commit()
        return identifier, root

    return create


@pytest.mark.parametrize("actor", [None, "owner", "allowed", "stranger", "moderator", "admin"])
@pytest.mark.parametrize(
    "case", ["public", "private", "hidden", "uploading", "processing", "failed", "deleted"]
)
def test_metadata_watch_and_files_enforce_same_acl(
    video_app, video_client, actors, media_video, actor, case
):
    values = {"visibility": "public" if case == "public" else "private"}
    if case in ("uploading", "processing", "failed"):
        values["processing_status"] = case
    if case == "hidden":
        values["moderation_status"] = "hidden"
    if case == "deleted":
        values["deleted_at"] = datetime.now(UTC)
    identifier, _ = media_video(**values)
    with video_app.state.session_factory() as db:
        db.add(
            VideoGrant(
                video_id=identifier,
                user_id=actors["allowed"]["id"],
                granted_by=actors["owner"]["id"],
            )
        )
        db.commit()
    act(video_client, actors, actor)
    permitted = case == "public" or (case == "private" and actor in ("owner", "allowed"))
    for suffix in ("", "/stream", "/thumbnail"):
        for method in ("GET", "HEAD") if suffix else ("GET",):
            response = video_client.request(
                method,
                f"/api/videos/{identifier}{suffix}",
                headers={"Range": "bytes=0-3", "If-None-Match": "*"},
            )
            assert response.status_code == (200 if permitted else 404)
            assert "no-store" in response.headers["cache-control"]
            if suffix and permitted:
                assert response.headers["x-accel-redirect"].startswith("/_protected_media/")
                assert response.headers["content-type"] == (
                    "video/mp4" if suffix == "/stream" else "image/jpeg"
                )
            if not permitted:
                assert "x-accel-redirect" not in response.headers
    page = video_client.get(f"/watch/{identifier}")
    assert page.status_code == (200 if permitted else 404)
    assert "no-store" in page.headers["cache-control"]
    assert ('id="video-player"' in page.text) == permitted
    assert ("Управление видео и доступом" in page.text) == (permitted and actor == "owner")


@pytest.mark.parametrize(
    "corruption", ["missing", "size", "key", "source", "symlink", "directory_symlink", "fifo"]
)
def test_media_fails_closed_for_invalid_storage(
    video_app, video_client, actors, media_video, tmp_path, corruption
):
    identifier, root = media_video()
    act(video_client, actors, "owner")
    target = root / "playback.mp4"
    if corruption == "missing":
        target.unlink()
    elif corruption == "size":
        target.write_bytes(b"changed")
    elif corruption in ("symlink", "fifo"):
        target.unlink()
        if corruption == "symlink":
            outside = tmp_path / "outside"
            outside.write_bytes(b"private outside file")
            target.symlink_to(outside)
        else:
            os.mkfifo(target)
    elif corruption == "directory_symlink":
        destination = tmp_path / "moved"
        root.rename(destination)
        root.symlink_to(destination, target_is_directory=True)
    else:
        with video_app.state.session_factory() as db:
            video = db.get(Video, identifier)
            video.playback_key = (
                "../../outside" if corruption == "key" else f"processing/{root.name}/source.mp4"
            )
            db.commit()
    for method in ("GET", "HEAD"):
        response = video_client.request(method, f"/api/videos/{identifier}/stream")
        assert response.status_code == 503
        assert "x-accel-redirect" not in response.headers
        assert "no-store" in response.headers["cache-control"]
    act(video_client, actors, "stranger")
    assert video_client.get(f"/api/videos/{identifier}/stream").status_code == 404


def test_revoke_and_visibility_change_apply_to_conditional_file_requests(
    video_app, video_client, actors, media_video
):
    identifier, _ = media_video(visibility="public")
    act(video_client, actors, None)
    assert video_client.get(f"/api/videos/{identifier}/stream").status_code == 200
    headers = act(video_client, actors, "owner")
    assert (
        video_client.patch(
            f"/api/videos/{identifier}", json={"visibility": "private"}, headers=headers
        ).status_code
        == 200
    )
    allowed = actors["allowed"]["id"]
    assert (
        video_client.put(f"/api/videos/{identifier}/grants/{allowed}", headers=headers).status_code
        == 200
    )
    act(video_client, actors, "allowed")
    assert video_client.get(f"/api/videos/{identifier}/stream").status_code == 200
    headers = act(video_client, actors, "owner")
    assert (
        video_client.delete(
            f"/api/videos/{identifier}/grants/{allowed}", headers=headers
        ).status_code
        == 204
    )
    for name in (None, "allowed"):
        act(video_client, actors, name)
        for suffix in ("stream", "thumbnail"):
            for method in ("GET", "HEAD"):
                response = video_client.request(
                    method,
                    f"/api/videos/{identifier}/{suffix}",
                    headers={
                        "Range": "bytes=0-1",
                        "If-Modified-Since": "Wed, 18 Nov 2037 00:00:00 GMT",
                        "If-None-Match": "*",
                    },
                )
                assert response.status_code == 404 and "x-accel-redirect" not in response.headers


def test_delete_requires_owner_and_csrf_and_is_idempotent(video_client, actors, media_video):
    identifier, _ = media_video()
    url = f"/api/videos/{identifier}"
    for name in (None, "stranger", "moderator", "admin", "allowed"):
        headers = act(video_client, actors, name)
        assert video_client.delete(url, headers=headers).status_code == (
            401 if name is None else 404
        )
    headers = act(video_client, actors, "owner")
    assert video_client.delete(url).status_code == 403
    assert video_client.delete(url, headers=headers).status_code == 204
    assert video_client.delete(url, headers=headers).status_code == 204
    assert video_client.get(url + "/status").status_code == 404
    assert video_client.patch(url, json={"title": "Revive"}, headers=headers).status_code == 404


def test_watch_escapes_metadata_and_uses_protected_urls(video_client, actors, media_video):
    identifier, _ = media_video(
        title='<script>alert("x")</script>', description="<img src=x onerror=alert(1)>"
    )
    act(video_client, actors, "owner")
    response = video_client.get(f"/watch/{identifier}")
    assert response.status_code == 200
    assert '<script>alert("x")</script>' not in response.text
    assert "&lt;script&gt;" in response.text and "&lt;img" in response.text
    assert f"/api/videos/{identifier}/stream" in response.text
    assert f"/api/videos/{identifier}/thumbnail" in response.text
