import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "release_check", Path(__file__).parents[1] / "scripts/release_check.py"
)
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


def evidence():
    checks = {name: {"status": "passed"} for name in release.GROUPS}
    checks["tests"].update(passed=10, failed=0, skipped=0)
    checks["policy"].update(
        registration="open",
        email_verification=True,
        password_recovery=True,
        backup_retention_days=30,
    )
    return {"format": 1, "source_fingerprint": "current", "checks": checks}


def test_local_evidence_cannot_silently_approve_public_release():
    result = release.assess(evidence(), "current")
    assert result["local_acceptance"] == "passed"
    assert result["public_release"] == "blocked" and not result["published"]
    assert all(status == "unrun" for status in result["external"].values())


def test_skipped_failed_missing_and_stale_results_do_not_pass():
    for status in ["failed", "skipped", "unrun", "unknown"]:
        item = evidence()
        item["checks"]["faults"]["status"] = status
        assert release.assess(item, "current")["local_acceptance"] == "failed"
    assert release.assess(evidence(), "changed")["local_acceptance"] == "failed"
    item = evidence()
    del item["checks"]["logs"]
    assert release.assess(item, "current")["local_acceptance"] == "failed"
    item = evidence()
    item["checks"]["tests"]["passed"] = 0
    assert release.assess(item, "current")["local_acceptance"] == "failed"


def test_all_external_evidence_is_required_and_policy_is_enforced():
    item = evidence()
    item["external"] = {name: "passed" for name in release.EXTERNAL}
    assert release.assess(item, "current")["public_release"] == "ready"
    item["checks"]["policy"]["registration"] = "disabled"
    assert release.assess(item, "current")["public_release"] == "blocked"


def test_version_metadata_matches_api_release_candidate():
    import tomllib

    from app import __version__

    root = Path(__file__).parents[1]
    assert tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"] == __version__
    locked = tomllib.loads((root / "uv.lock").read_text())
    assert (
        next(
            package["version"]
            for package in locked["package"]
            if package["name"] == "video-platform"
        )
        == __version__
    )
