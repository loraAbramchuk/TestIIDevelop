"""Populate only the disposable frontend_yt_preview database for UI checks."""

import getpass
import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

import httpx
from sqlalchemy import func, select

from app import cli
from app.config import Settings
from app.db import make_engine, make_session_factory
from app.models import User


def main():
    base = Path("/tmp/frontend-yt")
    settings = Settings()
    assert settings.postgres_db == "frontend_yt_preview", "Disposable database required"
    assert settings.media_root.resolve() == base / "media", "Disposable media root required"
    assert not (base / "accounts.json").exists(), "Fixture already exists; preserve it"
    engine = make_engine(settings)
    with make_session_factory(engine)() as db:
        assert db.scalar(select(func.count()).select_from(User)) == 0, (
            "Use an empty fixture database"
        )
    engine.dispose()
    base.mkdir(parents=True, exist_ok=True)
    password = secrets.token_urlsafe(24)
    getpass.getpass = lambda _: password
    sys.argv = [
        "cli",
        "create-admin",
        "--email",
        "owner@frontend.example",
        "--name",
        "Кадр за кадром",
    ]
    cli.main()
    clients = {
        role: httpx.Client(base_url="http://127.0.0.1:8098", trust_env=False)
        for role in ["owner", "moderator", "viewer"]
    }

    def write(client, path, method="POST", **kwargs):
        headers = {"X-CSRF-Token": client.get("/api/auth/csrf").json()["csrf_token"]}
        if method == "PUT" and "content" in kwargs:
            headers["Content-Type"] = "application/octet-stream"
        response = client.request(method, path, headers=headers, **kwargs)
        response.raise_for_status()
        return response

    write(
        clients["owner"],
        "/api/auth/login",
        json={"email": "owner@frontend.example", "password": password},
    )
    for role, name in [("moderator", "Другая перспектива"), ("viewer", "Александр")]:
        write(
            clients[role],
            "/api/auth/register",
            json={"email": role + "@frontend.example", "password": password, "display_name": name},
        )
    userids = {role: client.get("/api/auth/me").json()["id"] for role, client in clients.items()}
    write(
        clients["owner"],
        f"/api/admin/users/{userids['moderator']}/role",
        "PATCH",
        json={"role": "moderator", "reason": "Проверка интерфейса модератора"},
    )
    entries = [
        ("Как создать своё первое видео: от идеи до публикации", "owner", 0),
        ("Цвет, свет и немного вдохновения", "moderator", 55),
        ("Маленькие истории большого города", "owner", 120),
        ("Учимся видеть красоту в простых вещах", "moderator", 180),
        ("За кадром: как всё устроено на самом деле", "owner", 240),
        ("Две минуты, чтобы посмотреть на мир иначе", "moderator", 300),
    ]
    videos = []
    for index, (title, role, hue) in enumerate(entries):
        file = base / f"sample-{index}.mp4"
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=640x360:rate=24:duration=2",
                "-vf",
                f"hue=h={hue}",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-pix_fmt",
                "yuv420p",
                "-an",
                str(file),
            ],
            check=True,
        )
        client = clients[role]
        video = write(
            client,
            "/api/videos",
            json={
                "title": title,
                "description": (
                    "Тестовый ролик для проверки нового интерфейса. "
                    "Видео загружено через API и обработано настоящим worker."
                ),
                "visibility": "public",
            },
        ).json()["id"]
        write(client, f"/api/videos/{video}/content", "PUT", content=file.read_bytes())
        for _ in range(120):
            state = client.get(f"/api/videos/{video}/status").json()
            if state["processing_status"] == "ready":
                break
            if state["processing_status"] == "failed":
                raise RuntimeError(state)
            time.sleep(0.5)
        assert state["processing_status"] == "ready"
        videos.append(video)
    private = write(
        clients["owner"], "/api/videos", json={"title": "Только для своих", "visibility": "private"}
    ).json()["id"]
    write(
        clients["owner"],
        f"/api/videos/{private}/content",
        "PUT",
        content=(base / "sample-0.mp4").read_bytes(),
    )
    for _ in range(120):
        if (
            clients["owner"].get(f"/api/videos/{private}/status").json()["processing_status"]
            == "ready"
        ):
            break
        time.sleep(0.5)
    for root, _dirs, files in os.walk(base / "media"):
        Path(root).chmod(0o755)
        for name in files:
            (Path(root) / name).chmod(0o644)
    playlist = write(
        clients["owner"], "/api/playlists", json={"title": "С чего всё начинается"}
    ).json()["id"]
    for video in videos[:3]:
        write(clients["owner"], f"/api/playlists/{playlist}/items/{video}", "PUT")
    channel = clients["owner"].get("/api/channels/me").json()["id"]
    proof = base / "accounts.json"
    proof.touch(mode=0o600)
    proof.write_text(
        json.dumps(
            {
                "password": password,
                "users": userids,
                "videos": videos,
                "private": private,
                "playlist": playlist,
                "channel": channel,
            }
        )
    )
    print(
        "Frontend fixture ready: six public videos, private video, three roles and playlist",
        flush=True,
    )


if __name__ == "__main__":
    main()
