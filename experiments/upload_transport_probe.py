"""HTTP/Nginx smoke probe. Run only against a disposable application database."""

import argparse
import json
import secrets
import socket
import time
from urllib.parse import urlsplit
from uuid import uuid4

import httpx


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8083")
    parser.add_argument("--max-bytes", type=int, default=4 * 1024**2)
    args = parser.parse_args()
    base = urlsplit(args.base)
    assert base.scheme == "http" and base.hostname in ("127.0.0.1", "localhost")
    with httpx.Client(base_url=args.base, trust_env=False, timeout=20) as client:
        csrf = client.get("/api/auth/csrf").json()["csrf_token"]
        response = client.post(
            "/api/auth/register",
            headers={"X-CSRF-Token": csrf},
            json={
                "email": f"transport-{uuid4().hex}@example.com",
                "display_name": "Transport probe",
                "password": secrets.token_urlsafe(24),
            },
        )
        assert response.status_code == 201, response.text
        csrf = client.get("/api/auth/csrf").json()["csrf_token"]
        headers = {"X-CSRF-Token": csrf, "Content-Type": "application/octet-stream"}

        def create():
            result = client.post(
                "/api/videos",
                headers={"X-CSRF-Token": csrf},
                json={"title": "HTTP transport probe"},
            )
            assert result.status_code == 201, result.text
            return result.json()["id"]

        # Synthetic payload tests transport only; it does not claim codec validation.
        large = create()
        response = client.put(
            f"/api/videos/{large}/content", content=b"x" * (2 * 1024**2), headers=headers
        )
        assert response.status_code == 202, response.text
        interrupted = create()
        cookie = "; ".join(f"{key}={value}" for key, value in client.cookies.items())
        request_headers = (
            f"PUT /api/videos/{interrupted}/content HTTP/1.1\r\nHost: {base.netloc}\r\n"
            f"Cookie: {cookie}\r\nX-CSRF-Token: {csrf}\r\n"
            "Content-Type: application/octet-stream\r\nContent-Length: 100000\r\n\r\n"
        )
        with socket.create_connection((base.hostname, base.port or 80), timeout=5) as connection:
            connection.sendall(request_headers.encode() + b"partial")
            time.sleep(0.15)
        deadline = time.monotonic() + 5
        while True:
            state = client.get(f"/api/videos/{interrupted}/status").json()
            if state["processing_status"] == "failed":
                break
            assert time.monotonic() < deadline, state
            time.sleep(0.05)
        oversized = create()
        with socket.create_connection((base.hostname, base.port or 80), timeout=5) as connection:
            connection.sendall(
                (
                    f"PUT /api/videos/{oversized}/content HTTP/1.1\r\nHost: {base.netloc}\r\n"
                    f"Cookie: {cookie}\r\nX-CSRF-Token: {csrf}\r\n"
                    "Content-Type: application/octet-stream\r\nExpect: 100-continue\r\n"
                    f"Content-Length: {args.max_bytes + 1}\r\n\r\n"
                ).encode()
            )
            status_line = connection.recv(4096).split(b"\r\n", 1)[0]
            assert b" 413 " in status_line, status_line
        assert (
            client.get(f"/api/videos/{oversized}/status").json()["processing_status"] == "uploading"
        )
        print(
            json.dumps(
                {
                    "result": "passed",
                    "large_video_id": large,
                    "interrupted_video_id": interrupted,
                    "oversized_video_id": oversized,
                    "checks": ["2 MiB through Nginx", "TCP disconnect", "early Nginx 413"],
                }
            )
        )


if __name__ == "__main__":
    main()
