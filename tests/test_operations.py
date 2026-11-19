import hashlib
import importlib.util
import io
import json
import subprocess
import sys
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("video_ops", SCRIPTS / "ops.py")
ops = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ops)


def context(tmp_path, project="test-ops"):
    return ops.Operations(
        SimpleNamespace(
            root=tmp_path, env_file=tmp_path / "test.env", project=project, production=False
        )
    )


def envelope(path, *, revision="0011", mutate=None):
    media = io.BytesIO()
    with tarfile.open(fileobj=media, mode="w") as archive:
        member = tarfile.TarInfo("file.mp4")
        member.size = 4
        member.uid = 10001
        member.gid = 10001
        archive.addfile(member, io.BytesIO(b"clip"))
    payloads = {
        "config.env": b"POSTGRES_PASSWORD=test-only-secret\n",
        "database.dump": b"dump",
        "media.tar": media.getvalue(),
    }
    manifest = {
        "format": 1,
        "postgres_major": 17,
        "alembic_revision": revision,
        "production": False,
        "files": {
            name: {"size": len(value), "sha256": hashlib.sha256(value).hexdigest()}
            for name, value in payloads.items()
        },
    }
    if mutate:
        mutate(manifest)
    payloads["manifest.json"] = json.dumps(manifest).encode()
    with tarfile.open(path, "w:gz") as archive:
        for name, value in payloads.items():
            member = tarfile.TarInfo(name)
            member.size = len(value)
            archive.addfile(member, io.BytesIO(value))


def test_env_initialization_preserves_existing_credentials_and_port(tmp_path, capsys, monkeypatch):
    ctx = context(tmp_path)
    ctx.env_file.write_text("POSTGRES_PASSWORD=original-secret\nHTTP_PORT=8123\n")
    original = ctx.env_file.read_bytes()
    monkeypatch.setattr(ctx, "values", lambda: {"SITE_DOMAIN": "video.example.com"})
    ctx.initialize({"SITE_DOMAIN": "video.example.com"})
    assert ctx.env_file.read_bytes() == original
    with pytest.raises(ops.OpsError):
        ctx.initialize({"SITE_DOMAIN": "other.example.com"})
    assert (
        ctx.env_file.read_bytes() == original and "original-secret" not in capsys.readouterr().out
    )


def test_new_env_is_private_and_uses_a_random_password(tmp_path):
    from init_env import create_env

    a = tmp_path / "a"
    b = tmp_path / "b"
    assert create_env(a) and create_env(b)
    assert a.stat().st_mode & 0o777 == 0o600 and a.read_bytes() != b.read_bytes()
    assert not create_env(a)


def test_env_symlink_is_rejected(tmp_path):
    target = tmp_path / "target"
    target.write_text("preserve")
    (tmp_path / "test.env").symlink_to(target)
    with pytest.raises(ops.OpsError):
        context(tmp_path)
    assert target.read_text() == "preserve"


@pytest.mark.parametrize("name", ["../escape", "/absolute", "a/../../b", "a\\b"])
def test_tar_paths_cannot_escape_destination(name):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        member = tarfile.TarInfo(name)
        archive.addfile(member, io.BytesIO())
    buffer.seek(0)
    with tarfile.open(fileobj=buffer) as archive, pytest.raises(ops.OpsError):
        ops.safe_members(archive)


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "duplicate"])
def test_media_archive_rejects_links_devices_and_duplicates(kind):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        member = tarfile.TarInfo("file")
        if kind == "symlink":
            member.type = tarfile.SYMTYPE
            member.linkname = "../outside"
        if kind == "hardlink":
            member.type = tarfile.LNKTYPE
            member.linkname = "/etc/passwd"
        if kind == "fifo":
            member.type = tarfile.FIFOTYPE
        archive.addfile(member)
        if kind == "duplicate":
            archive.addfile(member)
    buffer.seek(0)
    with tarfile.open(fileobj=buffer) as archive, pytest.raises(ops.OpsError):
        ops.safe_members(archive)


def test_tls_relative_links_are_allowed_but_escape_is_rejected():
    for target, allowed in [
        ("../../archive/video/fullchain.pem", True),
        ("../../../outside", False),
        ("/etc/passwd", False),
    ]:
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode="w") as archive:
            member = tarfile.TarInfo("live/video/fullchain.pem")
            member.type = tarfile.SYMTYPE
            member.linkname = target
            archive.addfile(member)
        data.seek(0)
        with tarfile.open(fileobj=data) as archive:
            if allowed:
                ops.safe_members(archive, links=True)
            else:
                with pytest.raises(ops.OpsError):
                    ops.safe_members(archive, links=True)


def test_bundle_checksums_and_payload_permissions(tmp_path):
    bundle = tmp_path / "bundle.tar"
    envelope(bundle)
    destination = tmp_path / "out"
    destination.mkdir()
    result = ops.unpack_bundle(bundle, destination)
    assert result["alembic_revision"] == "0011"
    assert (destination / "config.env").stat().st_mode & 0o777 == 0o600
    envelope(bundle, mutate=lambda m: m["files"]["database.dump"].update(sha256="0" * 64))
    with pytest.raises(ops.OpsError, match="checksum"):
        ops.unpack_bundle(bundle, destination)


def test_failed_backup_resumes_original_services_in_order(tmp_path, monkeypatch):
    ctx = context(tmp_path)
    calls = []
    monkeypatch.setattr(
        ctx,
        "services",
        lambda: [{"Service": s, "State": "running"} for s in ("app", "web", "upload-maintenance")],
    )
    monkeypatch.setattr(ctx, "compose", lambda *args, **kwargs: calls.append(args))
    with pytest.raises(RuntimeError):
        with ctx.quiesce():
            raise RuntimeError("copy failure")
    assert calls[0][0] == "stop"
    assert calls[1] == ("start", "app", "upload-maintenance")
    assert calls[2][-1] == "web" and all("worker" not in call for call in calls)


def test_missing_recipient_fails_before_stopping_services(tmp_path, monkeypatch):
    ctx = context(tmp_path)
    monkeypatch.setattr(ctx, "values", lambda: {})
    monkeypatch.setattr(ctx, "services", lambda: pytest.fail("must not stop services"))
    with pytest.raises(ops.OpsError, match="recipient"):
        ctx.backup(local_only=True)


def test_existing_restore_target_is_not_overwritten(tmp_path):
    ctx = context(tmp_path)
    ctx.env_file.write_text("valuable")
    with pytest.raises(ops.OpsError, match="empty target"):
        ctx.restore(tmp_path / "missing.age", tmp_path / "key")
    assert ctx.env_file.read_text() == "valuable"


def test_unknown_backup_schema_refused_before_target_creation(tmp_path, monkeypatch):
    ctx = context(tmp_path)
    bundle = tmp_path / "bundle.tar"
    envelope(bundle, revision="9999")

    def fake(command, **kwargs):
        if command[0] == "age":
            kwargs["stdout"].write(bundle.read_bytes())
        return ""

    monkeypatch.setattr(ops, "run", fake)
    with pytest.raises(ops.OpsError, match="unknown"):
        ctx.restore(tmp_path / "encrypted.age", tmp_path / "key")
    assert not ctx.env_file.exists()


def test_ssh_upload_quotes_paths_and_checks_ciphertext_hash(tmp_path, monkeypatch):
    ctx = context(tmp_path)
    archive = tmp_path / "copy.age"
    archive.write_bytes(b"encrypted payload")
    remote = tmp_path / "remote 'quoted' $(touch never)"
    captured = []

    def fake(command, **kwargs):
        captured.append(command)
        subprocess.run(["sh", "-c", command[-1]], stdin=kwargs["stdin"], check=True)
        return ""

    monkeypatch.setattr(ops, "run", fake)
    result = ctx.ship(
        archive, {"BACKUP_SSH_TARGET": "backup@host.test", "BACKUP_SSH_PATH": str(remote)}
    )
    assert Path(result).read_bytes() == archive.read_bytes()
    assert not (tmp_path / "never").exists()
    assert "StrictHostKeyChecking=yes" in captured[0] and "BatchMode=yes" in captured[0]


def test_untrusted_env_values_are_not_shell_expanded(tmp_path):
    file = tmp_path / "env"
    file.touch()
    with pytest.raises(ops.OpsError):
        ops.append_setting(file, "SITE_DOMAIN", "$(echo unsafe)")
    assert file.read_bytes() == b""


@pytest.mark.parametrize("phase", ["build", "migration"])
def test_update_failure_restores_only_before_migration(tmp_path, monkeypatch, phase, capsys):
    ctx = context(tmp_path)
    calls = []

    def fake_run(command, **kwargs):
        calls.append(tuple(command))
        if command[-2:] == ["status", "--porcelain"]:
            return ""
        if command[-2:] == ["rev-parse", "HEAD"]:
            return "previous"
        if command[-2:] == ["rev-parse", "FETCH_HEAD"]:
            return "next"
        return ""

    def compose(*args, **kwargs):
        calls.append(args)
        if args[0] == "build" and phase == "build":
            raise ops.OpsError("build failed")
        return ""

    monkeypatch.setattr(ops, "run", fake_run)
    monkeypatch.setattr(ctx, "compose", compose)
    monkeypatch.setattr(ctx, "services", lambda: [{"Service": "app", "State": "running"}])
    monkeypatch.setattr(ctx, "cid", lambda service: "")
    monkeypatch.setattr(ctx, "backup", lambda **kw: tmp_path / "verified.age")
    monkeypatch.setattr(ctx, "resume", lambda services: calls.append(("resumed", *services)))

    def failed_start(**kwargs):
        raise ops.OpsError("migration failed")

    monkeypatch.setattr(ctx, "start", failed_start)
    with pytest.raises(ops.OpsError):
        ctx.update("release")
    rollback = any(call[-3:] == ("checkout", "--detach", "previous") for call in calls)
    assert rollback == (phase == "build")
    assert (("resumed", "app") in calls) == (phase == "build")
    if phase == "migration":
        assert any(call[0] == "stop" for call in calls)
        assert "verified.age" in capsys.readouterr().err


@pytest.mark.parametrize("field,value", [("files", []), ("postgres_major", 16), ("format", 2)])
def test_malformed_or_incompatible_manifest_is_rejected(tmp_path, field, value):
    bundle = tmp_path / "bundle.tar"
    envelope(bundle, mutate=lambda m: m.update({field: value}))
    output = tmp_path / "out"
    output.mkdir()
    with pytest.raises(ops.OpsError):
        ops.unpack_bundle(bundle, output)


@pytest.mark.parametrize("stopped", [None, "web", "worker", "upload-maintenance"])
def test_status_reports_stopped_service_even_if_app_diagnostics_ok(
    tmp_path, monkeypatch, capsys, stopped
):
    ctx = context(tmp_path)
    monkeypatch.setattr(ctx, "values", lambda: {})
    monkeypatch.setattr(
        ctx,
        "services",
        lambda: [
            {"Service": name, "State": "exited" if name == stopped else "running", "Health": ""}
            for name in (*ops.WRITERS, "db")
        ],
    )
    monkeypatch.setattr(
        ctx, "compose", lambda *args, **kw: "" if args[0] == "logs" else '{"status":"ok"}'
    )
    assert ctx.status() == (1 if stopped else 0)
    assert json.loads(capsys.readouterr().out)["status"] == ("degraded" if stopped else "ok")


@pytest.mark.parametrize("success", [False, True])
def test_renew_reloads_only_after_certbot_success(tmp_path, monkeypatch, success):
    ctx = context(tmp_path)
    events = []
    monkeypatch.setattr(
        ctx,
        "values",
        lambda: {
            "SITE_DOMAIN": "video.example.com",
            "ACME_EMAIL": "ops@example.com",
            "TLS_ROOT": str(tmp_path / "tls"),
            "ACME_WEBROOT": str(tmp_path / "acme"),
        },
    )

    def run(command, **kwargs):
        events.append(tuple(command))
        if command[0] == "certbot" and not success:
            raise ops.OpsError("ACME unavailable")
        return ""

    monkeypatch.setattr(ops, "run", run)
    monkeypatch.setattr(ctx, "compose", lambda *args, **kw: events.append(args))
    if success:
        ctx.tls(renew=True)
        assert events[1][-2:] == ("nginx", "-t")
        assert events[2][-3:] == ("nginx", "-s", "reload")
    else:
        with pytest.raises(ops.OpsError):
            ctx.tls(renew=True)
        assert len(events) == 1
    assert events[0][:2] == ("certbot", "renew")
