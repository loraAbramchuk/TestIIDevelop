"""Disposable first-run acceptance: fresh config, real Mailpit and Docker demo seed."""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from init_env import create_env  # noqa: E402
from mvp_release_probe import Lab, private_json, wait_for  # noqa: E402

sys.path.insert(0, str(ROOT))
from app.seed import identifier  # noqa: E402


def main():
    lab = Lab(Path(sys.argv[1]))
    mail = "first-run-mailpit-" + lab.id
    mail_created = False
    try:
        fresh = lab.path / "fresh.env"
        create_env(fresh)
        with fresh.open("a") as file:
            file.write("HTTP_PORT=8099\nMAILPIT_HTTP_PORT=8026\nMAILPIT_SMTP_PORT=1027\n")
        config = json.loads(
            subprocess.check_output(
                [
                    "docker",
                    "compose",
                    "--env-file",
                    str(fresh),
                    "-f",
                    str(ROOT / "compose.yaml"),
                    "config",
                    "--format",
                    "json",
                ],
                cwd=ROOT,
            )
        )
        environment = config["services"]["app"]["environment"]
        assert environment["MAIL_BACKEND"] == "auto" and not environment["SMTP_HOST"]
        assert environment["AUTH_REQUIRE_VERIFIED_EMAIL"] == "true"
        assert environment["AUTH_MAIL_SECRET"]
        assert environment["MAILPIT_HOST"] == "mailpit"
        production = json.loads(
            subprocess.check_output(
                [
                    "docker",
                    "compose",
                    "--env-file",
                    str(fresh),
                    "-f",
                    str(ROOT / "compose.yaml"),
                    "-f",
                    str(ROOT / "compose.production.yaml"),
                    "config",
                    "--format",
                    "json",
                ],
                cwd=ROOT,
                env=os.environ | {"SITE_DOMAIN": "video.example.com"},
            )
        )
        for service in ("app", "worker", "migrate", "upload-maintenance"):
            assert production["services"][service]["environment"]["MAIL_BACKEND"] == "smtp"
        assert "mailpit" not in production["services"]
        lab.command(
            [
                "docker",
                "run",
                "-d",
                "--name",
                mail,
                "--label",
                lab.label,
                "-p",
                "127.0.0.1:8026:8025",
                "-p",
                "127.0.0.1:1027:1025",
                config["services"]["mailpit"]["image"],
            ]
        )
        mail_created = True
        wait_for(
            lambda: httpx.get("http://127.0.0.1:8026/readyz", trust_env=False).status_code == 200
        )
        lab.env.update(environment)
        # Isolate database/storage/ports; keep the default mail policy and generated key.
        lab.env.update(
            DB_HOST="127.0.0.1",
            DB_PORT="15434",
            POSTGRES_DB="stage12_mvp_probe",
            POSTGRES_USER="video",
            MEDIA_ROOT=str(lab.path / "media"),
            MAILPIT_HOST="127.0.0.1",
            MAILPIT_PORT="1027",
            MAILPIT_UI_URL="http://127.0.0.1:8026",
            MAX_UPLOAD_BYTES=str(64 * 1024**2),
            OWNER_MEDIA_QUOTA_BYTES=str(512 * 1024**2),
            MEDIA_CAPACITY_BYTES=str(2 * 1024**3),
            MEDIA_MIN_FREE_BYTES=str(128 * 1024**2),
            PROCESSING_LEASE_SECONDS="60",
            PROCESSING_HEARTBEAT_SECONDS="5",
        )
        lab.setup()
        with httpx.Client(base_url=lab.base, trust_env=False) as client:

            def write(path, data):
                csrf = client.get("/api/auth/csrf").json()["csrf_token"]
                return client.post("/api/auth/" + path, json=data, headers={"X-CSRF-Token": csrf})

            email = "fresh-reviewer@example.com"
            password = "Fresh reviewer password!2026"
            response = write(
                "register",
                {"email": email, "display_name": "Новый пользователь", "password": password},
            )
            assert response.status_code == 201, response.text
            assert response.json()["user"]["email_verified"] is False
            assert "тестовом почтовом ящике" in client.get("/").text
            assert write("logout", None).status_code == 204
            assert write("login", {"email": email, "password": password}).status_code == 200

            def mail_link(subject):
                messages = httpx.get(
                    "http://127.0.0.1:8026/api/v1/messages", trust_env=False
                ).json()["messages"]
                for message in messages:
                    if message["Subject"] == subject:
                        body = httpx.get(
                            "http://127.0.0.1:8026/api/v1/message/" + message["ID"], trust_env=False
                        ).json()
                        if email in json.dumps(body):
                            links = re.findall(
                                r"http://localhost:8099/[^\s]+#token=[\w.-]+", body["Text"]
                            )
                            if links:
                                return links[0]
                return None

            links = {}

            def received(subject, key):
                link = mail_link(subject)
                if link:
                    links[key] = link
                return bool(link)

            wait_for(lambda: received("Подтвердите email", "verify"))
            token = links["verify"].split("#token=", 1)[1]
            assert write("email/verify", {"token": token}).status_code == 200
            assert client.get("/api/auth/me").json()["email_verified"]
            assert write("password/request", {"email": email}).status_code == 202
            wait_for(lambda: received("Восстановление пароля", "reset"))
            token = links["reset"].split("#token=", 1)[1]
            new_password = "Recovered reviewer password!2026"
            assert (
                write("password/reset", {"token": token, "password": new_password}).status_code
                == 200
            )
            assert write("login", {"email": email, "password": password}).status_code == 401
            assert write("login", {"email": email, "password": new_password}).status_code == 200
        # Copy the private read-only source mount into the disposable container,
        # then drop to the production UID before running the CLI.
        env_file = lab.path / "docker-seed.env"
        keys = [
            "DB_HOST",
            "DB_PORT",
            "POSTGRES_DB",
            "POSTGRES_USER",
            "POSTGRES_PASSWORD",
            "MEDIA_ROOT",
            "AUTH_MAIL_SECRET",
            "AUTH_REQUIRE_VERIFIED_EMAIL",
            "MAIL_BACKEND",
            "MAILPIT_HOST",
            "MAILPIT_PORT",
            "MAILPIT_UI_URL",
            "PUBLIC_BASE_URL",
            "AUTH_COOKIE_SECURE",
        ]
        env_file.touch(mode=0o600)
        env_file.write_text("\n".join(key + "=" + lab.env[key] for key in keys) + "\n")
        command = [
            "docker",
            "run",
            "--rm",
            "--network",
            "host",
            "--user",
            "0",
            "--env-file",
            str(env_file),
            "--mount",
            f"type=bind,source={ROOT}/app,target=/seed-source,readonly",
            "video-platform-app:local",
            "python",
            "-c",
            "import os,sys,shutil;from pathlib import Path;"
            "shutil.copytree('/seed-source','/app/app',dirs_exist_ok=True,"
            "ignore=shutil.ignore_patterns('__pycache__'));"
            "[os.chown(p,10001,10001) for p in [Path('/app/app'),*Path('/app/app').rglob('*')]];"
            "os.setgid(10001);os.setuid(10001);"
            "os.execv(sys.executable,[sys.executable,'-m','app.seed','--demo',"
            "'--base-url','http://127.0.0.1:8014'])",
        ]
        lab.command(command, log="seed-first.log")
        lab.readable()
        with httpx.Client(base_url=lab.base, trust_env=False) as guest:
            videos = guest.get("/api/videos").json()["items"]
            assert len(videos) == 3
            video = videos[0]["id"]
            playback = guest.get(f"/api/videos/{video}/stream", headers={"Range": "bytes=0-1023"})
            assert playback.status_code == 206 and len(playback.content) == 1024
        lab.command(command, log="seed-repeat.log")
        assert len(httpx.get(lab.base + "/api/videos", trust_env=False).json()["items"]) == 3
        browser_env = os.environ | {
            "FIRST_RUN_BASE": lab.base,
            "FIRST_RUN_OUTPUT": str(lab.path),
            "FIRST_RUN_PRIVATE": str(identifier("video:private")),
        }
        lab.command(
            ["node", "experiments/first_run_browser_probe.cjs"], env=browser_env, log="browser.log"
        )
        lab.full_tests()
        report = {
            "status": "passed",
            "registration": 201,
            "first_login": 200,
            "email_confirmation": "passed via real Mailpit",
            "password_recovery": "passed",
            "docker_seed": "passed twice, no duplicates",
            "public_playable_videos": 3,
            "private_video": "passed",
            "all_roles": "passed in Chromium",
            "production_capture": "disabled",
            "full_tests_log": "tests.log",
        }
        private_json(lab.path / "first-run-results.json", report)
        print(json.dumps(report))
    finally:
        if mail_created:
            info = json.loads(subprocess.check_output(["docker", "inspect", mail]))[0]
            assert info["Config"]["Labels"]["video.stage12.run"] == lab.id
            subprocess.run(["docker", "rm", "-f", mail], check=True, stdout=subprocess.DEVNULL)
        lab.cleanup()


if __name__ == "__main__":
    main()
