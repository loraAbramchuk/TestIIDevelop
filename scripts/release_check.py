"""Assess recorded evidence; this command never deploys, publishes or pushes."""

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GROUPS = ("tests", "browser", "metrics", "faults", "logs", "policy")
EXTERNAL = ("ubuntu_vm_systemd", "public_acme_renewal", "offsite_restore", "production_smtp")
STATUSES = {"passed", "failed", "skipped", "unrun"}


def source_fingerprint(root=ROOT):
    files = []
    for name in ("app", "migrations", "scripts", "experiments", "tests", "deploy"):
        files.extend(
            p
            for p in (root / name).rglob("*")
            if p.is_file() and "__pycache__" not in p.parts and p.suffix not in (".pyc", ".pyo")
        )
    files.extend(
        p
        for p in root.iterdir()
        if p.is_file()
        and (
            p.name
            in ("Dockerfile", "pyproject.toml", "uv.lock", "requirements.txt", ".env.example")
            or p.name.startswith("compose.")
            and p.suffix == ".yaml"
        )
    )
    digest = hashlib.sha256()
    for path in sorted(files):
        if path.is_symlink():
            raise ValueError("Source symlinks are not accepted as release evidence")
        digest.update(
            path.relative_to(root).as_posix().encode() + b"\0" + path.read_bytes() + b"\0"
        )
    return digest.hexdigest()


def assess(evidence, fingerprint):
    reasons = []
    if evidence.get("format") != 1 or evidence.get("source_fingerprint") != fingerprint:
        reasons.append("Missing, unsupported or stale evidence for the current source tree")
    checks = evidence.get("checks", {})
    statuses = {}
    for name in GROUPS:
        item = checks.get(name, {})
        status = item.get("status", "unrun")
        statuses[name] = status if status in STATUSES else "failed"
        if statuses[name] != "passed":
            reasons.append(name + ": " + statuses[name])
    tests = checks.get("tests", {})
    if (
        type(tests.get("passed")) is not int
        or tests.get("passed", 0) <= 0
        or tests.get("failed") != 0
        or type(tests.get("skipped")) is not int
        or tests.get("skipped", -1) < 0
    ):
        reasons.append("A passing test count and zero failures are required")
    policy = checks.get("policy", {})
    if (
        policy.get("registration") != "open"
        or not policy.get("email_verification")
        or not policy.get("password_recovery")
        or policy.get("backup_retention_days") != 30
    ):
        reasons.append("The agreed registration/mail/retention policy is not satisfied")
    external = {name: evidence.get("external", {}).get(name, "unrun") for name in EXTERNAL}
    public_reasons = reasons + [
        name + ": " + status for name, status in external.items() if status != "passed"
    ]
    return {
        "format": 1,
        "local_acceptance": "passed" if not reasons else "failed",
        "public_release": "ready" if not public_reasons else "blocked",
        "checks": statuses,
        "external": external,
        "local_reasons": reasons,
        "public_reasons": public_reasons,
        "published": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path)
    parser.add_argument(
        "--public", action="store_true", help="Require external infrastructure evidence too"
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = assess(json.loads(args.evidence.read_text()), source_fingerprint())
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered)
    print(rendered, end="")
    return (
        0
        if result["public_release"] == "ready"
        or not args.public
        and result["local_acceptance"] == "passed"
        else 1
    )


if __name__ == "__main__":
    sys.exit(main())
