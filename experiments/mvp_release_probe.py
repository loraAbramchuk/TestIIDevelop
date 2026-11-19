"""Disposable localhost MVP acceptance: real SMTP/PostgreSQL/Nginx/worker/browser.

Creates only uniquely labelled containers/volumes and a NEW private output directory.
Never modifies the main .env or uses its database/media, backup host or email provider.
"""

import argparse
import asyncio
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import traceback
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import httpx  # noqa: E402
from release_check import source_fingerprint  # noqa: E402
from smtp_capture import CaptureSMTP  # noqa: E402


def wait_for(function, seconds=30):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            result = function()
            if result:
                return result
        except (httpx.HTTPError, OSError, json.JSONDecodeError):
            pass
        time.sleep(0.05)
    raise AssertionError("Acceptance condition timed out")


def private_json(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)


class Lab:
    def __init__(self, directory):
        self.path = directory.resolve()
        self.path.mkdir(mode=0o700, parents=True, exist_ok=False)
        self.id = uuid4().hex[:12]
        self.db_name = "stage12-mvp-" + self.id + "-db"
        self.web_name = "stage12-mvp-" + self.id + "-web"
        self.volume = "stage12-mvp-" + self.id + "-postgres"
        self.label = "video.stage12.run=" + self.id
        self.created = []
        self.processes = []
        self.clients = {}
        self.smtp = None
        self.worker = self.web = None
        self.samples = []
        self.videos = []
        self.password = secrets.token_urlsafe(24)
        self.private_title = "PrivateCanary-" + uuid4().hex
        self.env = os.environ.copy()
        self.env.update(
            DB_HOST="127.0.0.1",
            DB_PORT="15434",
            POSTGRES_USER="video",
            POSTGRES_PASSWORD=secrets.token_urlsafe(32),
            POSTGRES_DB="stage12_mvp_probe",
            MEDIA_ROOT=str(self.path / "media"),
            PYTHONPATH=str(ROOT),
            AUTH_MAIL_SECRET=secrets.token_urlsafe(48),
            AUTH_REQUIRE_VERIFIED_EMAIL="true",
            SMTP_HOST="127.0.0.1",
            SMTP_PORT="1026",
            SMTP_SECURITY="plain",
            SMTP_FROM="noreply@example.com",
            SMTP_USERNAME="",
            SMTP_PASSWORD="",
            PUBLIC_BASE_URL="http://127.0.0.1:8099",
            AUTH_COOKIE_SECURE="false",
            MAX_UPLOAD_BYTES=str(64 * 1024**2),
            OWNER_MEDIA_QUOTA_BYTES=str(512 * 1024**2),
            MEDIA_CAPACITY_BYTES=str(2 * 1024**3),
            MEDIA_MIN_FREE_BYTES=str(2 * 1024**3),
            PROCESSING_CONCURRENCY="1",
            PROCESSING_LEASE_SECONDS="6",
            PROCESSING_HEARTBEAT_SECONDS="1",
            PROCESSING_RETRY_SECONDS="1",
        )

    def command(self, command, *, env=None, log=None, timeout=180):
        with open(self.path / (log or "commands.log"), "ab") as out:
            result = subprocess.run(
                command, cwd=ROOT, env=env or self.env, stdout=out, stderr=out, timeout=timeout
            )
        if result.returncode:
            raise AssertionError("Acceptance command failed: " + Path(command[0]).name)

    def spawn(self, command, log, *, env=None, pin_cpu=False):
        if pin_cpu:
            command = ["taskset", "-c", str(min(os.sched_getaffinity(0))), *command]
        stream = open(self.path / log, "ab")
        try:
            child = subprocess.Popen(
                command,
                cwd=ROOT,
                env=env or self.env,
                stdout=stream,
                stderr=stream,
                start_new_session=True,
            )
        finally:
            stream.close()
        self.processes.append(child)
        return child

    @staticmethod
    def stop(child):
        if child and child.poll() is None:
            os.killpg(child.pid, signal.SIGTERM)
            try:
                child.wait(timeout=20)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=5)

    def setup(self):
        if shutil.disk_usage(self.path).free < 3 * 1024**3:
            raise RuntimeError("Need at least 3 GiB free for the disposable lab")
        for port in (15434, 8014, 8099, 1026):
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", port))
        self.command(["docker", "volume", "create", "--label", self.label, self.volume])
        self.created.append(("volume", self.volume))
        self.command(
            [
                "docker",
                "run",
                "--detach",
                "--name",
                self.db_name,
                "--label",
                self.label,
                "-e",
                "POSTGRES_PASSWORD",
                "-e",
                "POSTGRES_USER=video",
                "-e",
                "POSTGRES_DB=stage12_mvp_probe",
                "-p",
                "127.0.0.1:15434:5432",
                "--mount",
                f"type=volume,source={self.volume},target=/var/lib/postgresql/data",
                "postgres:17-bookworm",
            ]
        )
        self.created.append(("container", self.db_name))

        def postgres_ready():
            return (
                subprocess.run(
                    ["docker", "exec", self.db_name, "pg_isready", "-U", "video"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                ).returncode
                == 0
            )

        wait_for(postgres_ready)
        self.command([sys.executable, "-m", "alembic", "upgrade", "head"])
        self.smtp = CaptureSMTP(self.path / "mail")
        self.smtp.start()
        (self.path / "media").mkdir(exist_ok=True, mode=0o755)
        template = (ROOT / "deploy/video-locations.conf.template").read_text()
        for key, value in {
            "MAX_UPLOAD_BYTES": self.env["MAX_UPLOAD_BYTES"],
            "UPLOAD_IDLE_TIMEOUT_SECONDS": "60",
            "UPLOAD_TIMEOUT_SECONDS": "3600",
            "HSTS_HEADER": "",
        }.items():
            template = template.replace("${" + key + "}", value)
        prefix = "upstream video_api { server 127.0.0.1:8014; }\n"
        production = (ROOT / "deploy/nginx.conf.template").read_text()
        prefix += production[production.index("map $request_uri") : production.index("server {")]
        prefix += "server { listen 127.0.0.1:8099;\n"
        (self.path / "nginx.conf").write_text(prefix + template + "\n}\n")
        self.web = self.spawn(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                "8014",
                "--no-access-log",
            ],
            "app.log",
        )
        self.command(
            [
                "docker",
                "run",
                "--detach",
                "--name",
                self.web_name,
                "--label",
                self.label,
                "--network",
                "host",
                "--mount",
                f"type=bind,source={self.path}/nginx.conf,target=/etc/nginx/conf.d/default.conf,readonly",
                "--mount",
                f"type=bind,source={self.path}/media,target=/data/media,readonly",
                "--entrypoint",
                "nginx",
                "video-platform-web:local",
                "-g",
                "daemon off;",
            ]
        )
        self.created.append(("container", self.web_name))
        self.base = "http://127.0.0.1:8099"
        wait_for(lambda: httpx.get(self.base + "/health/ready", trust_env=False).status_code == 200)
        self.start_worker()

    def start_worker(self):
        self.worker = self.spawn(
            [sys.executable, "-m", "app.processing.worker"], "worker.log", pin_cpu=True
        )
        wait_for(
            lambda: (
                httpx.get(self.base + "/health/diagnostics", trust_env=False).json()["checks"][
                    "processing"
                ]
                == "ok"
            )
        )

    def seed(self):
        code = """
import getpass,sys,json,os
from app.accounts import add_user_with_channel
from app.config import Settings
from app.db import make_engine,make_session_factory
from app.security import hash_password
settings=Settings();engine=make_engine(settings);result={}
with make_session_factory(engine)() as db:
 roles=[('owner','user'),('granted','user'),('outsider','user'),
        ('moderator','moderator'),('admin','admin')]
 for name,role in roles:
  user=add_user_with_channel(db,email=name+'@example.com',
      display_name='Acceptance '+name,role=role,
      password_hash=hash_password(os.environ['STAGE12_TEST_PASSWORD']))
  result[name]=str(user.id)
 db.commit()
fd=os.open(os.environ['STAGE12_USERS_FILE'],os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
with os.fdopen(fd,'w') as out:json.dump(result,out)
engine.dispose()
"""
        env = self.env | {
            "STAGE12_TEST_PASSWORD": self.password,
            "STAGE12_USERS_FILE": str(self.path / "users.json"),
        }
        self.command([sys.executable, "-c", code], env=env)
        self.users = json.loads((self.path / "users.json").read_text())
        for name in [*self.users, "guest"]:
            client = httpx.Client(base_url=self.base, trust_env=False, timeout=40)
            self.clients[name] = client
            if name != "guest":
                assert (
                    self.write(
                        name,
                        "/api/auth/login",
                        data={"email": name + "@example.com", "password": self.password},
                    ).status_code
                    == 200
                )

    def write(self, actor, path, method="POST", data=None, content=None):
        client = self.clients[actor]
        token = client.get("/api/auth/csrf").json()["csrf_token"]
        headers = {"X-CSRF-Token": token}
        if content is not None:
            headers["Content-Type"] = "application/octet-stream"
        return client.request(method, path, headers=headers, json=data, content=content)

    def create(self, actor="owner", title="Acceptance clip", visibility="private"):
        response = self.write(actor, "/api/videos", data={"title": title, "visibility": visibility})
        assert response.status_code == 201
        return response.json()["id"]

    def upload(self, identifier, sample, actor="owner"):
        response = self.write(
            actor, f"/api/videos/{identifier}/content", "PUT", content=sample.read_bytes()
        )
        assert response.status_code == 202

    def ready(self, identifier, actor="owner"):
        def state():
            response = self.clients[actor].get(f"/api/videos/{identifier}/status")
            assert response.status_code == 200
            current = response.json()["processing_status"]
            assert current != "failed"
            return current == "ready"

        wait_for(state, seconds=90)
        self.readable()

    def readable(self):
        # Host Python and container Nginx have different UIDs; only this synthetic fixture
        # gets read permission. Production uses the same app/media UID for all services.
        (self.path / "media").chmod(0o755)
        root = self.path / "media/processing"
        if root.exists():
            root.chmod(0o755)
            for directory in root.iterdir():
                if directory.is_dir():
                    directory.chmod(0o755)
                    for name in ("playback.mp4", "thumbnail.jpg"):
                        file = directory / name
                        if file.exists():
                            file.chmod(0o644)

    def make_samples(self):
        profiles = [("720p", 1280, 720, 8), ("1080p", 1920, 1080, 30), ("portrait", 720, 1280, 12)]
        for name, width, height, duration in profiles:
            path = self.path / (name + ".mp4")
            self.command(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    f"testsrc2=size={width}x{height}:rate=30:duration={duration}",
                    "-f",
                    "lavfi",
                    "-i",
                    f"sine=frequency=440:sample_rate=48000:duration={duration}",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "ultrafast",
                    "-pix_fmt",
                    "yuv420p",
                    "-threads",
                    "1",
                    "-c:a",
                    "aac",
                    "-shortest",
                    str(path),
                ]
            )
            self.samples.append(
                {
                    "name": name,
                    "width": width,
                    "height": height,
                    "duration_seconds": duration,
                    "path": path,
                }
            )

    def measure_processing(self):
        measurements = []
        for sample in self.samples:
            identifier = self.create(
                title=self.private_title
                if sample["name"] == "portrait"
                else "Synthetic " + sample["name"],
                visibility="private" if sample["name"] == "portrait" else "public",
            )
            start = time.monotonic()
            self.upload(identifier, sample["path"])
            accepted = time.monotonic()
            self.ready(identifier)
            measurements.append(
                {key: value for key, value in sample.items() if key != "path"}
                | {
                    "input_bytes": sample["path"].stat().st_size,
                    "upload_seconds": round(accepted - start, 4),
                    "accepted_to_ready_seconds": round(time.monotonic() - accepted, 4),
                }
            )
            self.videos.append(identifier)
        return measurements

    async def concurrent_uploads(self):
        release = asyncio.Event()
        started = 0
        lock = asyncio.Lock()
        items = [(name, self.create(name)) for name in ["owner", "owner", "outsider", "outsider"]]
        payload = self.samples[0]["path"].read_bytes()

        async def transfer(name, identifier):
            nonlocal started
            client = self.clients[name]
            csrf = client.get("/api/auth/csrf").json()["csrf_token"]

            async def body():
                nonlocal started
                yield payload[:1024]
                async with lock:
                    started += 1
                await release.wait()
                yield payload[1024:]

            async with httpx.AsyncClient(
                base_url=self.base, trust_env=False, timeout=60, cookies=client.cookies
            ) as connection:
                return await connection.put(
                    f"/api/videos/{identifier}/content",
                    content=body(),
                    headers={
                        "Content-Type": "application/octet-stream",
                        "X-CSRF-Token": csrf,
                        "Content-Length": str(len(payload)),
                    },
                )

        tasks = [asyncio.create_task(transfer(*item)) for item in items]
        end = time.monotonic() + 10
        try:
            while started < 4 and time.monotonic() < end:
                await asyncio.sleep(0.02)
            assert started == 4
            await asyncio.sleep(0.3)
            fifth = self.create("granted")
            rejected = await asyncio.to_thread(self.upload_response, "granted", fifth, payload)
            assert rejected.status_code == 429 and rejected.json()["code"] == "upload_slots_full"
        finally:
            release.set()
        responses = await asyncio.gather(*tasks)
        assert all(r.status_code == 202 for r in responses)
        for name, identifier in items:
            self.ready(identifier, name)
        # Finished concurrency samples are no longer needed. Release their quota and
        # disk reservations before the independent failure/recovery scenarios.
        for name, identifier in items:
            assert self.write(name, f"/api/videos/{identifier}", "DELETE").status_code == 204
        self.maintenance()
        return {
            "parallel_uploads": 4,
            "accepted": 4,
            "fifth_status": rejected.status_code,
            "processing_concurrency": 1,
        }

    def upload_response(self, name, identifier, payload):
        return self.write(name, f"/api/videos/{identifier}/content", "PUT", content=payload)

    async def measure_views(self):
        initial = self.clients["guest"].get(f"/api/videos/{self.videos[1]}/stream")
        assert initial.status_code == 200, (initial.status_code, len(initial.content))
        data = initial.content
        assert len(data) > 131072
        measurements = []
        for concurrency in (1, 4, 8, 16):
            async with httpx.AsyncClient(base_url=self.base, trust_env=False, timeout=20) as client:

                async def viewer(index):
                    durations = []
                    for count in range(12):
                        start = (index * 131072 + count * 262144) % (len(data) - 131072)
                        before = time.monotonic()
                        response = await client.get(
                            f"/api/videos/{self.videos[1]}/stream",
                            headers={"Range": f"bytes={start}-{start + 131071}"},
                        )
                        assert (
                            response.status_code == 206
                            and response.content == data[start : start + 131072]
                        ), (response.status_code, len(response.content), start)
                        durations.append(time.monotonic() - before)
                    return durations

                before = time.monotonic()
                values = sorted(
                    value
                    for group in await asyncio.gather(*(viewer(i) for i in range(concurrency)))
                    for value in group
                )
                elapsed = time.monotonic() - before
                measurements.append(
                    {
                        "concurrent_clients": concurrency,
                        "range_requests": len(values),
                        "range_bytes": 131072,
                        "errors": 0,
                        "seconds": round(elapsed, 4),
                        "p50_ms": round(values[len(values) // 2] * 1000, 3),
                        "p95_ms": round(
                            values[min(len(values) - 1, int(len(values) * 0.95))] * 1000, 3
                        ),
                        "requests_per_second": round(len(values) / elapsed, 2),
                    }
                )
        streams = []
        expected = hashlib.sha256(data).digest()
        for concurrency in (1, 4, 8):
            async with httpx.AsyncClient(base_url=self.base, trust_env=False, timeout=30) as client:

                async def download():
                    digest = hashlib.sha256()
                    size = 0
                    async with client.stream(
                        "GET", f"/api/videos/{self.videos[1]}/stream"
                    ) as response:
                        assert response.status_code == 200
                        async for chunk in response.aiter_bytes(1024**2):
                            digest.update(chunk)
                            size += len(chunk)
                    assert digest.digest() == expected and size == len(data)
                    return size

                before = time.monotonic()
                sizes = await asyncio.gather(*(download() for _ in range(concurrency)))
                elapsed = time.monotonic() - before
                streams.append(
                    {
                        "concurrent_clients": concurrency,
                        "bytes": sum(sizes),
                        "seconds": round(elapsed, 4),
                        "mib_per_second": round(sum(sizes) / 1024**2 / elapsed, 2),
                        "hashes_match": True,
                    }
                )
        return {"ranges": measurements, "complete_mp4": streams}

    def media_child(self):
        for path in Path("/proc").iterdir():
            if not path.name.isdigit():
                continue
            try:
                state = (path / "stat").read_text().split()
                command = (path / "cmdline").read_bytes().split(b"\0")
                if (
                    int(state[3]) == self.worker.pid
                    and state[2] != "Z"
                    and command
                    and Path(os.fsdecode(command[0])).name in ("ffmpeg", "ffprobe")
                ):
                    return int(path.name)
            except (OSError, ValueError, IndexError):
                pass
        return None

    def maintenance(self):
        self.command([sys.executable, "-m", "app.uploads.maintenance", "--once"], timeout=30)

    def rights(self):
        private = self.videos[2]
        path = "/api/videos/" + private
        checks = []
        for name in ("guest", "granted", "outsider", "moderator", "admin"):
            for suffix in ("", "/stream", "/thumbnail", "/comments", "/likes"):
                assert self.clients[name].get(path + suffix).status_code == 404
            assert (
                self.clients[name]
                .get(path + "/stream", headers={"Range": "bytes=0-99"})
                .status_code
                == 404
            )
        assert (
            self.write("owner", path + "/grants/" + self.users["granted"], "PUT").status_code == 200
        )
        assert (
            self.clients["granted"]
            .get(path + "/stream", headers={"Range": "bytes=0-99"})
            .status_code
            == 206
        )
        stop = threading.Event()
        statuses = []

        def reader():
            while not stop.is_set():
                statuses.append(
                    self.clients["granted"]
                    .get(path + "/stream", headers={"Range": "bytes=0-99"})
                    .status_code
                )

        reader_thread = threading.Thread(target=reader)
        reader_thread.start()
        try:
            assert (
                self.write("owner", path + "/grants/" + self.users["granted"], "DELETE").status_code
                == 204
            )
            for _ in range(20):
                assert (
                    self.clients["granted"]
                    .get(path + "/stream", headers={"Range": "bytes=0-99"})
                    .status_code
                    == 404
                )
        finally:
            stop.set()
            reader_thread.join(timeout=10)
        assert not reader_thread.is_alive() and set(statuses) <= {206, 404}
        checks.append("Six-actor private access matrix; grant revoke during parallel Range reads")
        for actor in ("guest", "owner", "granted", "outsider", "moderator"):
            assert self.clients[actor].get("/api/admin/users").status_code == (
                401 if actor == "guest" else 403
            )
        for actor in ("owner", "outsider", "granted"):
            assert self.clients[actor].get("/api/moderation/reports").status_code == 403
        checks.append("User/moderator/admin permissions and private grants remain independent")
        return checks

    def video_crash(self):
        os.kill(self.worker.pid, signal.SIGSTOP)
        identifier = self.create(title="Crash recovery clip")
        self.upload(identifier, self.samples[1]["path"])
        os.kill(self.worker.pid, signal.SIGCONT)
        media_pid = wait_for(self.media_child, seconds=10)
        self.worker.kill()
        self.worker.wait(timeout=5)

        def stopped():
            try:
                return Path(f"/proc/{media_pid}/stat").read_text().split()[2] == "Z"
            except FileNotFoundError:
                return True

        wait_for(stopped, seconds=5)
        time.sleep(6.2)
        self.maintenance()
        self.start_worker()
        self.ready(identifier)
        assert self.write("owner", f"/api/videos/{identifier}", "DELETE").status_code == 204
        self.maintenance()
        return "SIGKILL during actual FFmpeg/ffprobe; child killed, fenced retry reaches ready"

    def mail_crash(self):
        self.smtp.delivery_gate.clear()
        self.smtp.received.clear()
        client = httpx.Client(base_url=self.base, trust_env=False, timeout=30)
        try:
            csrf = client.get("/api/auth/csrf").json()["csrf_token"]
            address = "mail-crash-" + uuid4().hex + "@example.com"
            response = client.post(
                "/api/auth/register",
                json={"email": address, "password": self.password, "display_name": "Mail recovery"},
                headers={"X-CSRF-Token": csrf},
            )
            assert response.status_code == 201
            assert self.smtp.received.wait(timeout=15)
            self.worker.kill()
            self.worker.wait(timeout=5)
            self.smtp.delivery_gate.set()
            time.sleep(6.2)
            self.maintenance()
            self.start_worker()

            def delivered():
                with self.smtp.lock:
                    matches = [m for m in self.smtp.messages if m["to"] == address]
                    return matches if len(matches) >= 2 else None

            messages = wait_for(delivered)
            assert messages[0]["body"] == messages[1]["body"]
            value = re.search(r"#token=([A-Za-z0-9_.-]+)", messages[0]["body"]).group(1)
            csrf = client.get("/api/auth/csrf").json()["csrf_token"]
            assert (
                client.post(
                    "/api/auth/email/verify", json={"token": value}, headers={"X-CSRF-Token": csrf}
                ).status_code
                == 200
            )
            assert (
                client.post(
                    "/api/auth/email/verify", json={"token": value}, headers={"X-CSRF-Token": csrf}
                ).status_code
                == 400
            )
            return (
                "SIGKILL between SMTP acceptance and acknowledgement; "
                "same one-use link retried safely"
            )
        finally:
            self.smtp.delivery_gate.set()
            client.close()

    def database_restart(self):
        before = hashlib.sha256(
            self.clients["guest"].get(f"/api/videos/{self.videos[0]}/stream").content
        ).hexdigest()
        self.stop(self.worker)
        self.command(["docker", "stop", self.db_name])
        assert httpx.get(self.base + "/health/live", trust_env=False).status_code == 200
        assert httpx.get(self.base + "/health/ready", trust_env=False).status_code == 503
        assert httpx.get(self.base + "/api/videos", trust_env=False).status_code == 503
        self.command(["docker", "start", self.db_name])
        wait_for(
            lambda: (
                httpx.get(self.base + "/health/ready", trust_env=False, timeout=10).status_code
                == 200
            )
        )
        self.start_worker()
        after = hashlib.sha256(
            self.clients["guest"].get(f"/api/videos/{self.videos[0]}/stream").content
        ).hexdigest()
        assert before == after
        return (
            "Only the isolated PostgreSQL stopped/restarted; live stays 200, "
            "readiness/API 503, bytes survive"
        )

    def storage_failures(self):
        self.stop(self.web)
        env = self.env | {"MEDIA_MIN_FREE_BYTES": str(2**63 - 1)}
        self.web = self.spawn(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                "8014",
                "--no-access-log",
            ],
            "app.log",
            env=env,
        )
        wait_for(lambda: httpx.get(self.base + "/health/ready", trust_env=False).status_code == 200)
        assert (
            httpx.get(self.base + "/health/diagnostics", trust_env=False).json()["checks"][
                "disk_reserve"
            ]
            == "low"
        )
        identifier = self.create(title="Rejected disk reservation")
        assert (
            self.upload_response(
                "owner", identifier, self.samples[0]["path"].read_bytes()
            ).status_code
            == 507
        )
        assert self.clients["guest"].get(f"/api/videos/{self.videos[0]}/stream").status_code == 200
        self.stop(self.web)
        self.web = self.spawn(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                "8014",
                "--no-access-log",
            ],
            "app.log",
        )
        wait_for(
            lambda: (
                httpx.get(self.base + "/health/diagnostics", trust_env=False).json()["checks"][
                    "disk_reserve"
                ]
                == "ok"
            )
        )
        directories = [
            self.path / "media",
            *(p for p in (self.path / "media").rglob("*") if p.is_dir()),
        ]
        modes = {p: p.stat().st_mode & 0o777 for p in directories}
        try:
            for path in directories:
                path.chmod(0o555)
            diagnostics = httpx.get(self.base + "/health/diagnostics", trust_env=False).json()
            assert diagnostics["checks"]["storage_write"] == "error"
            assert httpx.get(self.base + "/health/ready", trust_env=False).status_code == 200
            assert (
                self.clients["guest"].get(f"/api/videos/{self.videos[0]}/stream").status_code == 200
            )
        finally:
            for path, mode in modes.items():
                if path.exists():
                    path.chmod(mode)
        return (
            "Configured low-space reserve rejects 507; real read-only directories "
            "degrade writes while ready/media stay available"
        )

    def deletion_race(self):
        os.kill(self.worker.pid, signal.SIGSTOP)
        identifier = self.create(title="Deleted during processing")
        self.upload(identifier, self.samples[1]["path"])
        os.kill(self.worker.pid, signal.SIGCONT)
        wait_for(self.media_child, seconds=10)
        assert self.write("owner", f"/api/videos/{identifier}", "DELETE").status_code == 204
        for _ in range(10):
            assert self.clients["owner"].get(f"/api/videos/{identifier}/stream").status_code == 404
        time.sleep(0.3)
        self.maintenance()
        return (
            "Deletion during actual media processing fences old work "
            "and immediately closes new reads"
        )

    def browser_context(self):
        playlist = self.write(
            "owner", "/api/playlists", data={"title": "Acceptance playlist"}
        ).json()["id"]
        for identifier in self.videos:
            assert (
                self.write(
                    "owner", f"/api/playlists/{playlist}/items/{identifier}", "PUT"
                ).status_code
                == 204
            )
        channel = self.clients["owner"].get("/api/channels/me").json()["id"]
        private_json(
            self.path / "context.json",
            {
                "base": self.base,
                "password": self.password,
                "users": self.users,
                "videos": self.videos,
                "playlist": playlist,
                "channel": channel,
                "sample": str(self.samples[0]["path"]),
                "inbox": str(self.path / "mail/inbox.json"),
                "media": str(self.path / "media"),
            },
        )

    def full_tests(self):
        self.command(["docker", "exec", self.db_name, "createdb", "-U", "video", "stage12_test"])
        # Only DB connectivity is shared with the lab. Its deliberately smaller quotas
        # and shorter leases must not override default-policy assertions in the suite.
        env = os.environ.copy()
        for key in ("DB_HOST", "DB_PORT", "POSTGRES_USER", "POSTGRES_PASSWORD"):
            env[key] = self.env[key]
        env.update(POSTGRES_DB="stage12_test", RUN_DB_TESTS="1")
        self.command([sys.executable, "-m", "alembic", "upgrade", "head"], env=env)
        self.command([sys.executable, "-m", "pytest", "-q"], env=env, log="tests.log", timeout=900)
        content = (self.path / "tests.log").read_text()
        match = re.search(r"(\d+) passed", content)
        assert match
        skipped = re.search(r"(\d+) skipped", content)
        return {
            "status": "passed",
            "passed": int(match[1]),
            "failed": 0,
            "skipped": int(skipped[1]) if skipped else 0,
        }

    def check_logs(self):
        self.command(["docker", "logs", self.web_name], log="nginx.log")
        self.command(["docker", "logs", self.db_name], log="postgres.log")
        forbidden = [self.password, self.env["POSTGRES_PASSWORD"], self.env["AUTH_MAIL_SECRET"]]
        forbidden.append(self.private_title)
        forbidden.extend(name + "@example.com" for name in self.users)
        from app.security import csrf_token

        for client in self.clients.values():
            for cookie in client.cookies.jar:
                forbidden.extend((cookie.value, csrf_token(cookie.value)))
        browser_canaries = self.path / "privacy-canaries.json"
        if browser_canaries.exists():
            forbidden.extend(json.loads(browser_canaries.read_text()))
        forbidden.extend(
            csrf_token(value)
            for value in list(forbidden)
            if re.fullmatch(r"[A-Za-z0-9_-]{43}", value)
        )
        with self.smtp.lock:
            for message in self.smtp.messages:
                forbidden.append(re.search(r"#token=([A-Za-z0-9_.-]+)", message["body"]).group(1))
                forbidden.append(message["to"])
        # The private fixture context and captured emails are deliberately not application logs.
        files = [
            self.path / name
            for name in (
                "app.log",
                "worker.log",
                "nginx.log",
                "postgres.log",
                "commands.log",
                "tests.log",
                "browser.log",
            )
            if (self.path / name).exists()
        ]
        for path in files:
            content = path.read_text(errors="replace")
            assert not any(value and value in content for value in forbidden), (
                "Sensitive canary in " + path.name
            )
            assert not re.search(
                r"(?:/_protected_media|processing|uploads)/[0-9a-f]{32}/", content
            ), "Storage key in " + path.name
        return {
            "status": "passed",
            "files": len(files),
            "checked_canaries": len(forbidden),
            "exception": (
                "Private fixture context/captured mail are 0600 "
                "and intentionally contain synthetic links"
            ),
        }

    def cleanup(self):
        for client in self.clients.values():
            client.close()
        for child in reversed(self.processes):
            self.stop(child)
        if self.smtp:
            self.smtp.close()
        for kind, name in reversed(self.created):
            inspect = (
                ["docker", "inspect", name]
                if kind == "container"
                else ["docker", "volume", "inspect", name]
            )
            item = json.loads(subprocess.check_output(inspect))[0]
            labels = (
                item.get("Config", {}).get("Labels", {})
                if kind == "container"
                else item.get("Labels", {})
            )
            assert labels.get("video.stage12.run") == self.id
            self.command(
                ["docker", "rm", "--force", name]
                if kind == "container"
                else ["docker", "volume", "rm", name]
            )
        if (self.path / "media").exists():
            shutil.rmtree(self.path / "media")
        for name in ("720p.mp4", "1080p.mp4", "portrait.mp4"):
            (self.path / name).unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=Path("/tmp") / ("stage12-mvp-" + uuid4().hex[:8])
    )
    parser.add_argument(
        "--skip-tests", action="store_true", help="Debug only: evidence records tests as unrun"
    )
    parser.add_argument(
        "--skip-faults", action="store_true", help="Debug only: evidence records faults as unrun"
    )
    args = parser.parse_args()
    lab = Lab(args.output)
    evidence = {
        "format": 1,
        "source_fingerprint": source_fingerprint(),
        "created_at": datetime.now(UTC).isoformat(),
        "checks": {},
        "external": {
            name: "unrun"
            for name in (
                "ubuntu_vm_systemd",
                "public_acme_renewal",
                "offsite_restore",
                "production_smtp",
            )
        },
    }
    stage = "setup"
    try:
        print("stage12: isolated setup", flush=True)
        lab.setup()
        lab.seed()
        lab.make_samples()
        print("stage12: processing and concurrent HTTP measurements", flush=True)
        stage = "metrics"
        samples = lab.measure_processing()
        uploads = asyncio.run(lab.concurrent_uploads())
        views = asyncio.run(lab.measure_views())
        evidence["checks"]["metrics"] = {
            "status": "passed",
            "samples": samples,
            "uploads": uploads,
            "views": views,
            "conditions": {
                "network": "loopback",
                "cache": "warm",
                "worker_cpu_affinity": 1,
                "logical_cpus_available": len(os.sched_getaffinity(0)),
                "upload_limit_bytes": int(lab.env["MAX_UPLOAD_BYTES"]),
                "free_disk_bytes": shutil.disk_usage(lab.path).free,
            },
        }
        print("stage12: browser and mail lifecycle", flush=True)
        stage = "browser"
        lab.browser_context()
        lab.command(
            ["node", "experiments/mvp_browser_probe.cjs", str(lab.path)],
            log="browser.log",
            timeout=180,
        )
        evidence["checks"]["browser"] = json.loads((lab.path / "browser.json").read_text())
        if args.skip_faults:
            evidence["checks"]["faults"] = {"status": "unrun"}
        else:
            print("stage12: access races, worker/mail/database/storage failures", flush=True)
            stage = "faults"
            cases = lab.rights() + [
                lab.video_crash(),
                lab.mail_crash(),
                lab.database_restart(),
                lab.storage_failures(),
                lab.deletion_race(),
            ]
            evidence["checks"]["faults"] = {"status": "passed", "cases": cases}
        if args.skip_tests:
            evidence["checks"]["tests"] = {
                "status": "unrun",
                "passed": 0,
                "failed": 0,
                "skipped": 0,
            }
        else:
            print("stage12: full suite in a second isolated database", flush=True)
            stage = "tests"
            evidence["checks"]["tests"] = lab.full_tests()
        evidence["checks"]["policy"] = {
            "status": "passed",
            "registration": "open",
            "email_verification": True,
            "password_recovery": True,
            "backup_retention_days": 30,
            "deletion": (
                "Immediate access revocation; asynchronous physical cleanup; "
                "backups may retain deleted data up to 30 days"
            ),
        }
        stage = "logs"
        evidence["checks"]["logs"] = lab.check_logs()
        stage = "source"
        assert source_fingerprint() == evidence["source_fingerprint"], (
            "Source changed during verification; rerun current source"
        )
        (lab.path / "evidence.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2) + "\n"
        )
        print("stage12: evidence " + str(lab.path / "evidence.json"), flush=True)
    except Exception as error:
        if stage in ("metrics", "browser", "faults", "tests", "logs"):
            evidence["checks"][stage] = {"status": "failed", "error_type": type(error).__name__}
        for name in ("metrics", "browser", "faults", "tests", "logs", "policy"):
            evidence["checks"].setdefault(name, {"status": "unrun"})
        (lab.path / "evidence.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2) + "\n"
        )
        for name, log in (
            (lab.web_name, "nginx-failure.log"),
            (lab.db_name, "postgres-failure.log"),
        ):
            if ("container", name) in lab.created:
                try:
                    lab.command(["docker", "logs", name], log=log)
                except Exception:
                    pass
        (lab.path / "failure.log").write_text(traceback.format_exc())
        print(
            "stage12 failed ("
            + type(error).__name__
            + "); private details: "
            + str(lab.path / "failure.log"),
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    finally:
        lab.cleanup()


if __name__ == "__main__":
    main()
