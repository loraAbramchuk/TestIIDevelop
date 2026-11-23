import contextlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.seed import SeedError, validate_local

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from ops import Operations, OpsError  # noqa: E402


def settings(**values):
    values.setdefault("auth_require_verified_email", True)
    return Settings(_env_file=None, postgres_password="first-run-unit-password", **values)


def test_fresh_local_configuration_can_send_without_an_external_provider():
    configured = settings(auth_mail_secret="m" * 48)
    assert configured.local_mail and configured.mail_configured
    assert configured.auth_require_verified_email
    assert not configured.smtp_host


def test_configured_provider_keeps_priority_over_local_capture():
    configured = settings(
        auth_mail_secret="m" * 48, smtp_host="smtp.example.com", smtp_from="noreply@example.com"
    )
    assert not configured.local_mail and configured.mail_configured


@pytest.mark.parametrize(
    "values",
    [
        {"mail_backend": "smtp"},
        {"auth_cookie_secure": True},
        {"public_base_url": "https://video.example.com"},
    ],
)
def test_public_or_explicit_smtp_never_falls_back_to_capture(values):
    configured = settings(auth_mail_secret="m" * 48, **values)
    assert not configured.local_mail and not configured.mail_configured
    with pytest.raises(SeedError):
        validate_local(configured, "http://127.0.0.1:8000")


def test_capture_cannot_be_pointed_at_a_public_mail_server():
    with pytest.raises(ValueError, match="Mailpit must use loopback"):
        settings(mailpit_host="smtp.example.com")
    with pytest.raises(ValueError, match="Mailpit requires"):
        settings(mail_backend="mailpit", public_base_url="https://video.example.com")


def test_local_launcher_prepares_missing_key_and_starts_mailpit(tmp_path, monkeypatch):
    path = tmp_path / "config.env"
    original = "POSTGRES_PASSWORD=preserved-db-password\nHTTP_PORT=8123\n"
    path.write_text(original)
    ctx = Operations(
        SimpleNamespace(root=tmp_path, env_file=path, project="first-run", production=False)
    )
    environment = {"MAIL_BACKEND": "auto", "PUBLIC_BASE_URL": "http://localhost:8123"}

    def config():
        values = dict(line.split("=", 1) for line in path.read_text().splitlines() if "=" in line)
        return {"services": {"app": {"environment": environment | values}}}

    monkeypatch.setattr(ctx, "config", config)
    calls = []
    monkeypatch.setattr(ctx, "compose", lambda *args, **kw: calls.append(args))
    monkeypatch.setattr(ctx, "quiesce", lambda **kw: contextlib.nullcontext())
    monkeypatch.setattr(ctx, "check", lambda: {"mail_delivery": "configured"})
    monkeypatch.setattr(ctx, "save", lambda **kw: None)
    monkeypatch.setattr("ops.run", lambda *a, **kw: "")
    ctx.start(build=False)
    assert path.read_text().startswith(original) and "AUTH_MAIL_SECRET=" in path.read_text()
    assert any(args[0] == "up" and "mailpit" in args for args in calls)
    content = path.read_bytes()
    ctx.start(build=False)
    assert path.read_bytes() == content


def test_incomplete_public_smtp_fails_before_build_or_stopping_services(tmp_path, monkeypatch):
    ctx = Operations(
        SimpleNamespace(root=tmp_path, env_file=tmp_path / "env", project="public", production=True)
    )
    monkeypatch.setattr(
        ctx,
        "config",
        lambda: {
            "services": {
                "app": {
                    "environment": {
                        "MAIL_BACKEND": "smtp",
                        "AUTH_MAIL_SECRET": "m" * 48,
                        "AUTH_COOKIE_SECURE": "true",
                        "PUBLIC_BASE_URL": "https://video.example.com",
                    }
                }
            }
        },
    )
    monkeypatch.setattr(
        ctx, "compose", lambda *a, **kw: pytest.fail("No mutation before preflight")
    )
    with pytest.raises(OpsError, match="SMTP is incomplete"):
        ctx.start()


@pytest.mark.parametrize("inherited", ["", "kept-non-empty-key-" + "x" * 32])
def test_launcher_uses_generated_key_for_empty_override_without_mutating_parent(
    tmp_path, monkeypatch, inherited
):
    import json
    import os

    path = tmp_path / "env"
    path.write_text("POSTGRES_PASSWORD=preserved-db-password\n")
    ctx = Operations(
        SimpleNamespace(root=tmp_path, env_file=path, project="empty-key", production=False)
    )
    monkeypatch.setenv("AUTH_MAIL_SECRET", inherited)
    started_keys = []

    def run(command, **kwargs):
        if command[0] == "git":
            return ""
        child = kwargs.get("env") or os.environ
        values = dict(line.split("=", 1) for line in path.read_text().splitlines() if "=" in line)
        key = child.get("AUTH_MAIL_SECRET", values.get("AUTH_MAIL_SECRET", ""))
        if "config" in command:
            return json.dumps(
                {
                    "services": {
                        "app": {
                            "environment": {
                                "MAIL_BACKEND": "auto",
                                "AUTH_MAIL_SECRET": key,
                                "PUBLIC_BASE_URL": "http://localhost:8080",
                            }
                        }
                    }
                }
            )
        if "up" in command:
            started_keys.append(key)
        return ""

    monkeypatch.setattr("ops.run", run)
    monkeypatch.setattr(ctx, "quiesce", lambda **kw: contextlib.nullcontext())
    monkeypatch.setattr(ctx, "check", lambda: {"mail_delivery": "configured"})
    monkeypatch.setattr(ctx, "save", lambda **kw: None)
    ctx.start()
    assert started_keys and all(len(key) >= 32 for key in started_keys)
    assert os.environ["AUTH_MAIL_SECRET"] == inherited
    if inherited:
        assert started_keys == [inherited, inherited]
        assert "AUTH_MAIL_SECRET=" not in path.read_text()
    else:
        assert all("AUTH_MAIL_SECRET=" + key in path.read_text() for key in started_keys)


def test_bad_effective_key_fails_before_build(tmp_path, monkeypatch):
    ctx = Operations(
        SimpleNamespace(
            root=tmp_path, env_file=tmp_path / "env", project="bad-key", production=False
        )
    )
    monkeypatch.setattr(
        ctx,
        "config",
        lambda: {
            "services": {
                "app": {
                    "environment": {
                        "MAIL_BACKEND": "auto",
                        "AUTH_MAIL_SECRET": "too-short",
                    }
                }
            }
        },
    )
    monkeypatch.setattr(ctx, "compose", lambda *a, **kw: pytest.fail("No service mutation"))
    with pytest.raises(OpsError, match="effective Compose configuration"):
        ctx.start()


def test_launcher_does_not_report_success_without_running_mail_configuration(tmp_path, monkeypatch):
    ctx = Operations(
        SimpleNamespace(
            root=tmp_path, env_file=tmp_path / "env", project="runtime-key", production=False
        )
    )
    monkeypatch.setattr(
        ctx,
        "config",
        lambda: {
            "services": {
                "app": {
                    "environment": {
                        "MAIL_BACKEND": "auto",
                        "AUTH_MAIL_SECRET": "m" * 48,
                    }
                }
            }
        },
    )
    calls = []
    monkeypatch.setattr(ctx, "compose", lambda *args, **kw: calls.append(args))
    monkeypatch.setattr(ctx, "quiesce", lambda **kw: contextlib.nullcontext())
    monkeypatch.setattr(ctx, "check", lambda: {"status": "ok", "mail_delivery": "not_configured"})
    with pytest.raises(OpsError, match="running app"):
        ctx.start(build=False)
    assert not any(args[0] == "up" and args[-1] == "web" for args in calls)
