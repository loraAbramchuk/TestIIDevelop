import importlib.util
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings


@pytest.mark.parametrize(
    "url",
    [
        "https://user:pass@example.com",
        "https://example.com/path",
        "//example.com",
        "https://example.com?redirect=bad",
        "https://example.com#bad",
    ],
)
def test_link_origin_is_explicit_and_has_no_credentials_or_redirects(url):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, postgres_password="test-password-only", public_base_url=url)


def test_plain_smtp_is_only_for_loopback():
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            postgres_password="test-password-only",
            smtp_host="smtp.example.com",
            smtp_security="plain",
        )
    settings = Settings(
        _env_file=None,
        postgres_password="test-password-only",
        auth_mail_secret="",
        smtp_password="",
    )
    assert not settings.mail_configured


def test_default_production_policy_requires_verification():
    assert Settings.model_fields["auth_require_verified_email"].default is True


def test_prepare_mail_secret_preserves_credentials_is_private_and_idempotent(tmp_path, capsys):
    spec = importlib.util.spec_from_file_location(
        "mail_init_env", Path(__file__).parents[1] / "scripts/init_env.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    path = tmp_path / "config.env"
    original = (
        'POSTGRES_PASSWORD=existing-private-password\nSMTP_PASSWORD="existing smtp password"\n'
    )
    path.write_text(original)
    assert module.prepare_mail(path)
    content = path.read_text()
    assert content.startswith(original) and "AUTH_MAIL_SECRET=" in content
    assert path.stat().st_mode & 0o777 == 0o600
    assert not module.prepare_mail(path) and path.read_text() == content
    assert capsys.readouterr().out == ""
    alias = tmp_path / "alias.env"
    alias.symlink_to(path)
    with pytest.raises(OSError):
        module.prepare_mail(alias)
