"""Full encrypted off-site backup and restore probe. Disposable projects only."""

import hashlib
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
SUFFIX = os.getenv("STAGE11_TEST_SUFFIX", "")
assert SUFFIX in ("", "-fixes"), "Only disposable fixture names are allowed"
BASE = Path("/tmp/stage11-dr" + SUFFIX)
SOURCE = "stage11-source" + SUFFIX
TARGET = "stage11-restored" + SUFFIX
DOMAIN = "video.test"


def command(args, *, check=True, input=None, file=None):
    result = subprocess.run(
        args,
        cwd=ROOT,
        input=input,
        stdout=file or subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=file is None,
        check=False,
    )
    if check and result.returncode:
        error = (
            result.stderr.decode(errors="replace")
            if isinstance(result.stderr, bytes)
            else result.stderr
        )
        raise RuntimeError(f"{args[0]} failed: {error[-1000:]}")
    return result


def ops(project, envfile, *args, check=True):
    return command(
        [
            sys.executable,
            str(ROOT / "scripts/ops.py"),
            "--project",
            project,
            "--env-file",
            str(envfile),
            "--production",
            *map(str, args),
        ],
        check=check,
    )


def wait(fn, timeout=90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = fn()
        if result:
            return result
        time.sleep(0.5)
    raise AssertionError("Timed out waiting for expected state")


def main():
    assert os.getenv("STAGE11_TEST_PROJECT") == SOURCE, "Disposable project guard required"
    assert (BASE / "recovery-key").is_file(), "Prepare age/SSH test fixtures first"
    source_env = BASE / "source.env"
    target_env = BASE / "restored.env"
    assert not source_env.exists() and not target_env.exists(), "Use fresh fixture paths"
    for project in (SOURCE, TARGET):
        assert not command(
            [
                "docker",
                "volume",
                "ls",
                "-q",
                "--filter",
                f"label=com.docker.compose.project={project}",
            ]
        ).stdout.strip(), "Test target already has data"
    tls = BASE / "source-tls"
    certdir = tls / "archive" / DOMAIN
    certdir.mkdir(parents=True)
    live = tls / "live" / DOMAIN
    live.mkdir(parents=True)
    command(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "30",
            "-subj",
            "/CN=Stage11 test CA",
            "-keyout",
            str(BASE / "ca.key"),
            "-out",
            str(BASE / "ca.crt"),
        ]
    )
    command(
        [
            "openssl",
            "req",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-subj",
            "/CN=" + DOMAIN,
            "-keyout",
            str(certdir / "privkey1.pem"),
            "-out",
            str(BASE / "server.csr"),
        ]
    )
    (BASE / "extensions").write_text(
        "subjectAltName=DNS:video.test,IP:127.0.0.1\nextendedKeyUsage=serverAuth\n"
    )
    command(
        [
            "openssl",
            "x509",
            "-req",
            "-in",
            str(BASE / "server.csr"),
            "-CA",
            str(BASE / "ca.crt"),
            "-CAkey",
            str(BASE / "ca.key"),
            "-CAcreateserial",
            "-days",
            "30",
            "-extfile",
            str(BASE / "extensions"),
            "-out",
            str(certdir / "server.crt"),
        ]
    )
    (certdir / "fullchain1.pem").write_bytes(
        (certdir / "server.crt").read_bytes() + (BASE / "ca.crt").read_bytes()
    )
    (live / "fullchain.pem").symlink_to("../../archive/" + DOMAIN + "/fullchain1.pem")
    (live / "privkey.pem").symlink_to("../../archive/" + DOMAIN + "/privkey1.pem")
    (certdir / "cert1.pem").write_bytes((certdir / "server.crt").read_bytes())
    (certdir / "chain1.pem").write_bytes((BASE / "ca.crt").read_bytes())
    for name in ("cert", "chain"):
        (live / (name + ".pem")).symlink_to("../../archive/" + DOMAIN + "/" + name + "1.pem")
    renewal = tls / "renewal"
    renewal.mkdir()
    (renewal / (DOMAIN + ".conf")).write_text(
        "version = 2.9.0\narchive_dir = "
        + str(certdir)
        + "\n"
        + "".join(
            name + " = " + str(live / (name + ".pem")) + "\n"
            for name in ("cert", "privkey", "chain", "fullchain")
        )
        + "[renewalparams]\nauthenticator = webroot\n"
        + "server = https://acme-v02.api.letsencrypt.org/directory\n"
        + "webroot_path = "
        + str(BASE / "acme")
        + ",\n[[webroot_map]]\n"
        + DOMAIN
        + " = "
        + str(BASE / "acme")
        + "\n"
    )
    (certdir / "privkey1.pem").chmod(0o600)
    recipient = command(["age-keygen", "-y", str(BASE / "recovery-key")]).stdout.strip()
    settings = {
        "POSTGRES_PASSWORD": "isolated-stage11-database-password",
        "POSTGRES_USER": "video",
        "POSTGRES_DB": "stage11_data",
        "SITE_DOMAIN": DOMAIN,
        "ACME_EMAIL": "operator@example.com",
        "PUBLIC_BIND_ADDRESS": "127.0.0.1",
        "HTTP_PUBLIC_PORT": "8189",
        "HTTPS_PORT": "8449",
        "TLS_ROOT": str(tls),
        "ACME_WEBROOT": str(BASE / "acme"),
        "MAX_UPLOAD_BYTES": "4194304",
        "OWNER_MEDIA_QUOTA_BYTES": "67108864",
        "MEDIA_CAPACITY_BYTES": "536870912",
        "MEDIA_MIN_FREE_BYTES": "0",
        "PREVIEW_BUDGET_BYTES": "262144",
        "UPLOAD_CLEANUP_INTERVAL_SECONDS": "2",
        "PROCESSING_LEASE_SECONDS": "6",
        "PROCESSING_HEARTBEAT_SECONDS": "1",
        "PROCESSING_RETRY_SECONDS": "1",
        "BACKUP_RECIPIENT": recipient,
        "BACKUP_SSH_TARGET": "root@127.0.0.1",
        "BACKUP_SSH_PORT": "22228",
        "BACKUP_SSH_PATH": "/backups/video",
        "BACKUP_SSH_IDENTITY": str(BASE / "ssh-key"),
        "BACKUP_SSH_KNOWN_HOSTS": str(BASE / "known_hosts"),
    }
    (BASE / "acme").mkdir(mode=0o755, exist_ok=True)
    source_env.touch(mode=0o600)
    source_env.write_text("".join(f"{k}={json.dumps(v)}\n" for k, v in settings.items()))
    before_env = source_env.read_bytes()
    ops(SOURCE, source_env, "start", "--no-build")
    ops(SOURCE, source_env, "start", "--no-build")
    assert source_env.read_bytes() == before_env
    print("Source production stack and repeat start passed", flush=True)
    original_dns = socket.getaddrinfo
    socket.getaddrinfo = lambda host, *args, **kwargs: original_dns(
        "127.0.0.1" if host == DOMAIN else host, *args, **kwargs
    )
    tls_context = ssl.create_default_context(cafile=str(BASE / "ca.crt"))

    def client(port):
        return httpx.Client(
            base_url=f"https://{DOMAIN}:{port}", verify=tls_context, trust_env=False, timeout=30
        )

    owner = client(8449)
    allowed = client(8449)
    stranger = client(8449)
    guest = client(8449)
    password = "isolated-stage11-owner-password"
    envcommand = [
        "docker",
        "compose",
        "-p",
        SOURCE,
        "--env-file",
        str(source_env),
        "-f",
        "compose.yaml",
        "-f",
        "compose.production.yaml",
    ]
    bootstrap = (
        "import json,sys,getpass;from app import cli;x=json.load(sys.stdin);"
        "getpass.getpass=lambda _:x['password'];"
        "sys.argv=['cli','create-admin','--email','owner@example.com','--name','Owner'];"
        "cli.main()"
    )
    command(
        envcommand + ["exec", "-T", "app", "python", "-c", bootstrap],
        input=json.dumps({"password": password}),
    )

    def write(c, path, method="POST", payload=None, content=None):
        csrf = c.get("/api/auth/csrf")
        assert csrf.status_code == 200
        headers = {"X-CSRF-Token": csrf.json()["csrf_token"]}
        if content is not None:
            headers["Content-Type"] = "application/octet-stream"
        return c.request(method, path, json=payload, content=content, headers=headers)

    login = write(
        owner, "/api/auth/login", payload={"email": "owner@example.com", "password": password}
    )
    assert login.status_code == 200
    cookie = login.headers["set-cookie"]
    assert "__Host-video_session=" in cookie and "Secure" in cookie and "Domain=" not in cookie
    for c, email in [(allowed, "allowed@example.com"), (stranger, "stranger@example.com")]:
        assert (
            write(
                c,
                "/api/auth/register",
                payload={"email": email, "display_name": email.split("@")[0], "password": password},
            ).status_code
            == 201
        )
    owner_id = owner.get("/api/auth/me").json()["id"]
    allowed_id = allowed.get("/api/auth/me").json()["id"]
    assert (
        write(
            owner,
            f"/api/admin/users/{allowed_id}/role",
            "PATCH",
            {"role": "moderator", "reason": "Preserved role history"},
        ).status_code
        == 200
    )
    media = Path("/tmp/stage1-media/h264-aac.mp4").read_bytes()

    def upload(title, visibility="private", ready=True):
        result = write(owner, "/api/videos", payload={"title": title, "visibility": visibility})
        assert result.status_code == 201
        identifier = result.json()["id"]
        result = write(owner, f"/api/videos/{identifier}/content", "PUT", content=media)
        assert result.status_code == 202, result.status_code
        if ready:
            wait(
                lambda: (
                    owner.get(f"/api/videos/{identifier}/status").json()["processing_status"]
                    == "ready"
                )
            )
        return identifier

    private = upload("Restored private video")
    public = upload("Restored public video", "public")
    hidden = upload("Restored hidden video", "public")
    assert write(owner, f"/api/videos/{private}/grants/{allowed_id}", "PUT").status_code == 200
    assert write(allowed, f"/api/videos/{private}/likes", "PUT").status_code == 200
    assert (
        write(
            allowed, f"/api/videos/{private}/comments", payload={"text": "Preserved comment"}
        ).status_code
        == 201
    )
    assert (
        write(
            allowed,
            f"/api/videos/{private}/reports",
            payload={"reason": "other", "explanation": "Preserved report"},
        ).status_code
        == 201
    )
    playlist = write(owner, "/api/playlists", payload={"title": "Preserved playlist"}).json()["id"]
    for identifier in (private, public):
        assert (
            write(owner, f"/api/playlists/{playlist}/items/{identifier}", "PUT").status_code == 204
        )
    assert (
        write(
            owner,
            f"/api/moderation/videos/{hidden}/visibility",
            payload={"status": "hidden", "reason": "Preserved moderation"},
        ).status_code
        == 200
    )
    expected_stream = hashlib.sha256(owner.get(f"/api/videos/{private}/stream").content).hexdigest()
    expected_thumb = hashlib.sha256(
        owner.get(f"/api/videos/{private}/thumbnail").content
    ).hexdigest()
    assert (
        allowed.get(f"/api/videos/{private}/stream", headers={"Range": "bytes=0-1"}).status_code
        == 206
    )
    assert guest.get(f"/api/videos/{private}/stream").status_code == 404
    assert owner.get(f"/api/videos/{hidden}").status_code == 404
    redirect = httpx.get("http://127.0.0.1:8189/api/auth/me", trust_env=False)
    assert (
        redirect.status_code == 308
        and redirect.headers["location"] == f"https://{DOMAIN}:8449/api/auth/me"
    )
    assert "max-age=" in owner.get("/").headers["strict-transport-security"]
    assert owner.get("/", headers={"Host": "wrong.test"}).status_code == 421
    assert owner.get("/").text.count("https://") > 0
    owner.get("/search", params={"q": "sensitive-query-marker"})
    assert (
        owner.get(
            f"/api/videos/{private}/stream", headers={"Range": "bytes=999999999-"}
        ).status_code
        == 416
    )
    status = json.loads(ops(SOURCE, source_env, "status").stdout)
    assert (
        status["status"] == "ok"
        and status["resources"]["live_workers"] >= 1
        and status["resources"]["disk"]["free"] > 0
    )
    logs = command(envcommand + ["logs", "--no-color", "app", "web"]).stdout
    assert (
        password not in logs
        and "sensitive-query-marker" not in logs
        and owner.cookies.get("__Host-video_session") not in logs
    )
    print("HTTPS, Secure cookies, log hygiene, metadata and protected media passed", flush=True)
    command(envcommand + ["stop", "worker"])
    wait(
        lambda: owner.get("/health/diagnostics").json()["checks"]["processing"] == "error",
        timeout=20,
    )
    assert owner.get("/health/ready").status_code == 200
    diagnostic = owner.get("/health/diagnostics").json()
    assert diagnostic["status"] == "degraded" and diagnostic["video_uploads"] == "available"
    assert (
        owner.get(f"/api/videos/{private}/stream", headers={"Range": "bytes=0-1"}).status_code
        == 206
    )
    queued = upload("Pending private job", ready=False)
    assert owner.get(f"/api/videos/{queued}/status").json()["processing_status"] == "processing"
    result = ops(SOURCE, source_env, "status", check=False)
    assert result.returncode == 1
    assert json.loads(result.stdout)["resources"]["live_workers"] == 0
    backup_dir = BASE / "local-backups"
    result = ops(SOURCE, source_env, "backup", "--destination", backup_dir)
    record = json.loads(result.stdout.strip().splitlines()[-1])
    archive = Path(record["backup"])
    assert record["offsite"] is True and archive.stat().st_mode & 0o777 == 0o600
    state = command(envcommand + ["ps", "--all", "--format", "json"]).stdout
    states = (
        json.loads(state)
        if state.lstrip().startswith("[")
        else [json.loads(line) for line in state.splitlines()]
    )
    assert any(s["Service"] == "worker" and s["State"] != "running" for s in states)
    assert owner.get("/health/ready").status_code == 200
    remote = "/backups/video/" + archive.name
    remote_sha = command(["docker", "exec", "stage11-offsite", "sha256sum", remote]).stdout.split()[
        0
    ]
    assert remote_sha == record["sha256"]
    assert (
        command(["docker", "exec", "stage11-offsite", "stat", "-c", "%a", remote]).stdout.strip()
        == "600"
    )
    print(
        "Consistent encrypted SSH backup passed; worker intentionally remains stopped", flush=True
    )
    command(envcommand + ["down", "--volumes"])
    source_env.unlink()
    shutil.rmtree(tls)
    archive.unlink()
    downloaded = BASE / "downloaded.tar.age"
    with downloaded.open("wb") as output:
        command(
            [
                "ssh",
                "-F",
                "/dev/null",
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=yes",
                "-o",
                "UserKnownHostsFile=" + str(BASE / "known_hosts"),
                "-i",
                str(BASE / "ssh-key"),
                "-p",
                "22228",
                "root@127.0.0.1",
                "cat " + remote,
            ],
            file=output,
        )
    downloaded.chmod(0o600)
    assert hashlib.sha256(downloaded.read_bytes()).hexdigest() == record["sha256"]
    corrupt = BASE / "corrupt.age"
    data = bytearray(downloaded.read_bytes())
    data[-12] ^= 1
    corrupt.write_bytes(data)
    refused = ops(
        "stage11-bad",
        BASE / "bad.env",
        "restore",
        corrupt,
        "--identity",
        BASE / "recovery-key",
        check=False,
    )
    assert refused.returncode != 0 and not (BASE / "bad.env").exists()
    ops(
        TARGET,
        target_env,
        "restore",
        downloaded,
        "--identity",
        BASE / "recovery-key",
        "--public-http-port",
        "8190",
        "--https-port",
        "8450",
    )
    restored = client(8450)
    restored.cookies.update(owner.cookies)
    recipient_client = client(8450)
    recipient_client.cookies.update(allowed.cookies)
    nobody = client(8450)
    assert restored.get("/api/auth/me").json()["id"] == owner_id
    assert recipient_client.get("/api/auth/me").json()["role"] == "moderator"
    assert restored.get(f"/api/videos/{private}").status_code == 200
    assert (
        hashlib.sha256(restored.get(f"/api/videos/{private}/stream").content).hexdigest()
        == expected_stream
    )
    assert (
        hashlib.sha256(restored.get(f"/api/videos/{private}/thumbnail").content).hexdigest()
        == expected_thumb
    )
    assert (
        recipient_client.get(
            f"/api/videos/{private}/stream", headers={"Range": "bytes=0-1"}
        ).status_code
        == 206
    )
    assert (
        nobody.get(f"/api/videos/{private}/stream", headers={"Range": "bytes=0-1"}).status_code
        == 404
    )
    assert (
        restored.get(f"/api/videos/{hidden}/status").json()["moderation_reason"]
        == "Preserved moderation"
    )
    assert restored.get(f"/api/videos/{hidden}").status_code == 404
    assert restored.get(f"/api/playlists/{playlist}").json()["visible_count"] == 2
    assert restored.get(f"/api/videos/{private}/comments").json()["total_count"] == 1
    assert restored.get(f"/api/videos/{private}/likes").json()["like_count"] == 1
    assert len(restored.get("/api/admin/audit").json()["items"]) == 1
    assert len(restored.get("/api/moderation/reports").json()["items"]) == 1
    wait(
        lambda: restored.get(f"/api/videos/{queued}/status").json()["processing_status"] == "ready"
    )
    assert (
        restored.get(f"/api/videos/{queued}/stream", headers={"Range": "bytes=0-1"}).status_code
        == 206
    )
    assert (
        write(restored, f"/api/videos/{private}/grants/{allowed_id}", "DELETE").status_code == 204
    )
    assert (
        recipient_client.get(
            f"/api/videos/{private}/stream", headers={"Range": "bytes=0-1"}
        ).status_code
        == 404
    )
    assert recipient_client.get(f"/api/playlists/{playlist}").json()["visible_count"] == 1
    second_restore = ops(
        TARGET, target_env, "restore", downloaded, "--identity", BASE / "recovery-key", check=False
    )
    assert second_restore.returncode != 0
    restored_tls = ROOT / ".local/operations" / TARGET / "letsencrypt"
    renewal_text = (restored_tls / "renewal" / (DOMAIN + ".conf")).read_text()
    assert str(tls) not in renewal_text
    assert str(restored_tls / "live" / DOMAIN / "cert.pem") in renewal_text
    assert str(ROOT / ".local/operations" / TARGET / "acme") in renewal_text
    deployed = json.loads(ops(TARGET, target_env, "status").stdout)
    assert deployed["status"] == "ok"
    native = BASE / "native.json"
    native.touch(mode=0o600)
    native.write_text(
        json.dumps(
            {
                "base": f"https://{DOMAIN}:8450",
                "email": "owner@example.com",
                "password": password,
                "video_id": private,
            }
        )
    )
    print(
        "Restore after source loss passed; private ACL, histories and pending job recovered",
        flush=True,
    )
    report = {
        "result": "passed",
        "checks": [
            "production HTTPS with verified fixture CA and Secure __Host cookie",
            "repeat start preserves env",
            "structured logs omit credentials/cookie/query",
            "worker stop keeps readiness/playback; queues new upload",
            "consistent age backup, SSH host-key check/hash/atomic remote file",
            "source DB/media/TLS/env removed before restore",
            "corrupted archive and nonempty target refused",
            "new independent project restores sessions/roles/grants/playlists/social/report/audit",
            "restored MP4/JPEG byte hashes, Range and private/hidden/revocation ACL",
            "pending processing job finishes after restore",
            "Certbot renewal paths relocated after original TLS directory removal",
            "operator status includes disk, queue, workers, HTTP errors and TLS expiry",
        ],
    }
    Path("/tmp/stage11-restore" + SUFFIX + ".json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report), flush=True)


if __name__ == "__main__":
    main()
