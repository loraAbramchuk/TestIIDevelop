"""Real Nginx delivery/lifecycle probe. Only the disposable stage5-verify project."""

import argparse
import hashlib
import json
import secrets
import subprocess
import tempfile
import time
from pathlib import Path
from uuid import UUID, uuid4

import httpx


def docker(*arguments):
    return subprocess.check_output(["docker", *arguments], text=True, timeout=40).strip()


def sql(query):
    return docker(
        "exec",
        "stage5-verify-db-1",
        "psql",
        "-U",
        "video",
        "-d",
        "stage5_verify",
        "-At",
        "-c",
        query,
    )


def wait_for(check, *, timeout=40):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = check()
            if result:
                return result
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    raise AssertionError("Timed out waiting for test state")


def register(base, name):
    client = httpx.Client(base_url=base, timeout=15)
    csrf = client.get("/api/auth/csrf").json()["csrf_token"]
    response = client.post(
        "/api/auth/register",
        headers={"X-CSRF-Token": csrf},
        json={
            "email": f"playback-{uuid4().hex}@example.com",
            "display_name": name,
            "password": secrets.token_hex(24),
        },
    )
    assert response.status_code == 201, response.text
    data = response.json()
    client.headers["X-CSRF-Token"] = data["csrf_token"]
    return client, data["user"]["id"]


def upload(client, data, title):
    created = client.post("/api/videos", json={"title": title, "visibility": "private"})
    assert created.status_code == 201
    identifier = str(UUID(created.json()["id"]))
    response = client.put(
        f"/api/videos/{identifier}/content",
        content=data,
        headers={"Content-Type": "application/octet-stream"},
    )
    assert response.status_code == 202, response.text
    return identifier


def ready(client, identifier):
    def check():
        status = client.get(f"/api/videos/{identifier}/status").json()
        assert status["processing_status"] != "failed", status
        return status["processing_status"] == "ready"

    wait_for(check)


def private_headers(response):
    assert "no-store" in response.headers["cache-control"]
    assert "cookie" in response.headers["vary"].lower()
    assert "x-accel-redirect" not in response.headers
    assert response.headers["x-content-type-options"] == "nosniff"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True, choices=["stage5-verify"])
    parser.add_argument("--media", type=Path, default=Path("/tmp/stage1-media/h264-aac.mp4"))
    args = parser.parse_args()
    base = "http://127.0.0.1:8089"
    owner, _ = register(base, "Владелец HTTP")
    allowed, allowed_id = register(base, "Получатель HTTP")
    guest = httpx.Client(base_url=base, timeout=15)
    checks = []
    try:
        identifier = upload(owner, args.media.read_bytes(), "HTTP playback acceptance")
        ready(owner, identifier)
        url = f"/api/videos/{identifier}"
        response = owner.get(url + "/stream")
        assert response.status_code == 200
        private_headers(response)
        assert response.headers["content-type"] == "video/mp4"
        assert response.headers["accept-ranges"] == "bytes"
        data = response.content
        size = len(data)
        assert int(response.headers["content-length"]) == size
        assert data.index(b"moov") < data.index(b"mdat")
        thumbnail = owner.get(url + "/thumbnail")
        assert thumbnail.status_code == 200 and thumbnail.content.startswith(b"\xff\xd8")
        assert thumbnail.headers["content-type"] == "image/jpeg"
        private_headers(thumbnail)
        for suffix, length in (("stream", size), ("thumbnail", len(thumbnail.content))):
            head = owner.head(url + "/" + suffix)
            assert head.status_code == 200 and not head.content
            assert int(head.headers["content-length"]) == length
            private_headers(head)
        for range_, start, end in (
            ("bytes=0-1023", 0, 1023),
            ("bytes=-128", size - 128, size - 1),
            ("bytes=128-", 128, size - 1),
        ):
            partial = owner.get(url + "/stream", headers={"Range": range_})
            assert partial.status_code == 206
            assert partial.content == data[start : end + 1]
            assert partial.headers["content-range"] == f"bytes {start}-{end}/{size}"
            assert int(partial.headers["content-length"]) == end - start + 1
            private_headers(partial)
        outside = owner.get(url + "/stream", headers={"Range": f"bytes={size}-"})
        assert outside.status_code == 416 and outside.headers["content-range"] == f"bytes */{size}"
        private_headers(outside)
        multiple = owner.get(url + "/stream", headers={"Range": "bytes=0-1,4-5"})
        assert multiple.status_code == 200 and multiple.content == data
        checks += [
            "GET/HEAD MP4/JPEG",
            "exact closed/open/suffix byte ranges",
            "416 outside file",
            "multiple ranges bounded by full response",
        ]

        key_rows = json.loads(
            sql(
                "SELECT json_build_array(source_key,playback_key,thumbnail_key) "
                f"FROM videos WHERE id='{identifier}'"
            )
        )
        internal = "/_protected_media/" + key_rows[1].removeprefix("processing/")
        for path in (
            internal,
            "/" + key_rows[0],
            "/" + key_rows[1],
            "/data/media/" + key_rows[1],
            internal.replace("playback.mp4", "playback.part"),
        ):
            assert guest.get(path).status_code == 404
        for client in (guest, allowed):
            for suffix in ("", "/stream", "/thumbnail"):
                for method in ("GET", "HEAD") if suffix else ("GET",):
                    denied = client.request(
                        method,
                        url + suffix,
                        headers={
                            "Range": "bytes=0-1",
                            "If-None-Match": "*",
                            "X-Accel-Redirect": internal,
                        },
                    )
                    assert denied.status_code == 404
                    private_headers(denied)
        assert owner.put(url + f"/grants/{allowed_id}").status_code == 200
        assert allowed.get(url + "/stream", headers={"Range": "bytes=0-1"}).content == data[:2]
        assert owner.delete(url + f"/grants/{allowed_id}").status_code == 204
        for suffix in ("", "/stream", "/thumbnail"):
            denied = allowed.get(
                url + suffix,
                headers={
                    "Range": "bytes=0-1",
                    "If-None-Match": "*",
                    "If-Modified-Since": "Wed, 18 Nov 2037 00:00:00 GMT",
                },
            )
            assert denied.status_code == 404
        assert owner.patch(url, json={"visibility": "public"}).status_code == 200
        assert guest.get(url + "/stream").status_code == 200
        assert owner.patch(url, json={"visibility": "private"}).status_code == 200
        assert guest.get(url + "/stream", headers={"If-None-Match": "*"}).status_code == 404
        checks += [
            "no direct/internal/source/partial-file access",
            "private ACL on every GET/HEAD/Range",
            "grant/revoke",
            "public to private with conditional headers",
        ]

        docker("stop", "stage5-verify-worker-1")
        wait_for(lambda: guest.get("/health/diagnostics").json()["checks"]["processing"] == "error")
        assert owner.get(url + "/stream").content == data
        assert guest.get("/health/ready").status_code == 200
        docker("restart", "stage5-verify-app-1", "stage5-verify-web-1")
        wait_for(lambda: guest.get("/health/ready").status_code == 200)
        assert owner.get(url + "/stream").content == data
        assert owner.get(url + "/thumbnail").content == thumbnail.content
        docker("start", "stage5-verify-worker-1")
        checks += [
            "worker outage does not stop playback/readiness",
            "app/Nginx restart preserves sessions and files",
        ]

        # Delete while a real media subprocess holds its source/output locks.
        with tempfile.TemporaryDirectory(prefix="stage5-long-") as directory:
            long_clip = Path(directory) / "long.mp4"
            subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-y",
                    "-stream_loop",
                    "49",
                    "-i",
                    str(args.media),
                    "-t",
                    "100",
                    "-c",
                    "copy",
                    str(long_clip),
                ],
                check=True,
                timeout=30,
            )
            active = upload(owner, long_clip.read_bytes(), "Delete active worker")
        wait_for(
            lambda: (
                sql(
                    "SELECT count(*) FROM processing_runs "
                    f"WHERE video_id='{active}' AND state='running'"
                )
                == "1"
            )
        )
        assert owner.delete(f"/api/videos/{active}").status_code == 204
        assert owner.get(f"/api/videos/{active}/stream").status_code == 404
        wait_for(
            lambda: (
                sql(
                    "SELECT count(*) FROM upload_attempts "
                    f"WHERE video_id='{active}' AND state='cleaned' AND reserved_bytes=0"
                )
                == "1"
            )
        )
        assert (
            sql(
                "SELECT playback_key IS NULL AND deleted_at IS NOT NULL "
                f"FROM videos WHERE id='{active}'"
            )
            == "t"
        )
        assert owner.get(url + "/stream").content == data
        checks.append("delete during real FFmpeg cancels publication and cleans files")

        docker("stop", "stage5-verify-upload-maintenance-1")
        assert owner.delete(url).status_code == 204
        assert owner.delete(url).status_code == 204
        assert (
            sql(
                "SELECT state='cleanup' AND reserved_bytes>0 FROM upload_attempts "
                f"WHERE video_id='{identifier}'"
            )
            == "t"
        )
        for suffix in ("", "/status", "/stream", "/thumbnail"):
            assert owner.get(url + suffix).status_code == 404
        docker("start", "stage5-verify-upload-maintenance-1")
        wait_for(
            lambda: (
                sql(
                    "SELECT state='cleaned' AND reserved_bytes=0 FROM upload_attempts "
                    f"WHERE video_id='{identifier}'"
                )
                == "t"
            )
        )
        existence = docker(
            "exec",
            "stage5-verify-app-1",
            "python",
            "-c",
            "import json,sys; from pathlib import Path; "
            "print(any((Path('/data/media')/p).exists() for p in json.loads(sys.argv[1])))",
            json.dumps(key_rows),
        )
        assert existence == "False"
        checks += [
            "delete immediately denies access",
            "cleanup survives maintenance restart",
            "quota released after file removal",
            "idempotent deletion",
        ]
        report = {
            "result": "passed",
            "checks": checks,
            "mp4_bytes": size,
            "sha256": hashlib.sha256(data).hexdigest(),
        }
        Path("/tmp/stage5-http.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report))
    finally:
        docker("start", "stage5-verify-worker-1", "stage5-verify-upload-maintenance-1")
        for client in (owner, allowed, guest):
            client.close()


if __name__ == "__main__":
    main()
