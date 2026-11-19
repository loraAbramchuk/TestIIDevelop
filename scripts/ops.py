#!/usr/bin/env python3
"""Local operator CLI. Standard library only; never source .env as shell code."""

import argparse
import ast
import configparser
import contextlib
import fcntl
import hashlib
import io
import json
import os
import posixpath
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from uuid import uuid4

from init_env import create_env
from ops_storage import (
    ARCHIVE_OVERHEAD,
    DEFAULT_RESERVE,
    GuardedReader,
    GuardedWriter,
    OpsError,
    SpaceBudget,
    stop_process,
    stream_command,
    tree_inventory,
)

WRITERS = ("web", "app", "worker", "upload-maintenance")
PAYLOADS = {"config.env", "database.dump", "media.tar", "tls.tar"}


def run(command, *, stdin=None, stdout=None, binary=False, timeout=3600, check=True, env=None):
    if isinstance(stdout, GuardedWriter) or isinstance(stdin, GuardedReader):
        destination = stdout if isinstance(stdout, GuardedWriter) else None
        return stream_command(command, destination, stdin=stdin, timeout=timeout, env=env)
    result = subprocess.run(
        command,
        stdin=stdin,
        stdout=stdout or subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=not binary,
        timeout=timeout,
        env=env,
    )
    if check and result.returncode:
        error = result.stderr.decode(errors="replace") if binary else result.stderr
        raise OpsError(f"{Path(command[0]).name} failed: {error.strip()[:500]}")
    return result.stdout or (b"" if binary else "")


def digest(path):
    with Path(path).open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + "." + uuid4().hex)
    try:
        with os.fdopen(
            os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w"
        ) as file:
            json.dump(data, file, sort_keys=True)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def safe_members(archive, *, links=False):
    seen = set()
    for member in archive.getmembers():
        path = PurePosixPath(member.name)
        if path.is_absolute() or ".." in path.parts or "\\" in member.name:
            raise OpsError("Unsafe archive path")
        normalized = str(path)
        if normalized in seen:
            raise OpsError("Duplicate archive entry")
        seen.add(normalized)
        if member.issym() and links:
            target = posixpath.normpath(posixpath.join(str(path.parent), member.linkname))
            if member.linkname.startswith("/") or target == ".." or target.startswith("../"):
                raise OpsError("Unsafe archive link")
        elif not (member.isfile() or member.isdir()):
            raise OpsError("Unsupported archive entry")
    return archive.getmembers()


def unpack_bundle(path, destination, *, reserve=DEFAULT_RESERVE):
    with tarfile.open(path, "r:*") as archive:
        members = safe_members(archive)
        names = {m.name for m in members}
        if not {"manifest.json", "config.env", "database.dump", "media.tar"} <= names or names - (
            PAYLOADS | {"manifest.json"}
        ):
            raise OpsError("Unsupported backup contents")
        budget = SpaceBudget(destination, reserve)
        budget.require(sum(member.size for member in members))
        for member in members:
            if not member.isfile() or "/" in member.name:
                raise OpsError("Backup envelope must contain regular files only")
            with (
                archive.extractfile(member) as source,
                (destination / member.name).open("wb") as output,
            ):
                shutil.copyfileobj(source, GuardedWriter(output, budget), length=1024**2)
            (destination / member.name).chmod(0o600)
    manifest = json.loads((destination / "manifest.json").read_text())
    if (
        not isinstance(manifest, dict)
        or type(manifest.get("format")) is not int
        or manifest["format"] != 1
        or manifest.get("postgres_major") != 17
    ):
        raise OpsError("Unsupported backup version")
    if not isinstance(manifest.get("files"), dict) or set(manifest["files"]) != names - {
        "manifest.json"
    }:
        raise OpsError("Backup manifest does not match payloads")
    if (
        not isinstance(manifest.get("alembic_revision"), str)
        or type(manifest.get("production")) is not bool
    ):
        raise OpsError("Invalid backup metadata")
    if manifest["production"] and "tls.tar" not in names:
        raise OpsError("TLS snapshot is missing")
    for name, info in manifest["files"].items():
        if (
            not isinstance(info, dict)
            or type(info.get("size")) is not int
            or info["size"] < 0
            or not isinstance(info.get("sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", info["sha256"])
        ):
            raise OpsError("Invalid backup file metadata")
        file = destination / name
        if file.stat().st_size != info["size"] or digest(file) != info["sha256"]:
            raise OpsError("Backup checksum mismatch")
    with tarfile.open(destination / "media.tar") as archive:
        safe_members(archive)
    if "tls.tar" in names:
        with tarfile.open(destination / "tls.tar") as archive:
            safe_members(archive, links=True)
    return manifest


def append_setting(path, key, value):
    if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or any(c in str(value) for c in "\r\n\x00$"):
        raise OpsError("Invalid deployment setting")
    with Path(path).open("a") as file:
        file.write(f"\n{key}={json.dumps(str(value), ensure_ascii=False)}\n")


def relocate_certbot(tls, webroot):
    """Rebase ConfigObj-compatible renewal files without changing ACME accounts."""
    for file in sorted((tls / "renewal").glob("*.conf")):
        if file.is_symlink():
            raise OpsError("Renewal configuration must be a regular file")
        parser = configparser.ConfigParser(interpolation=None)
        parser.optionxform = str
        try:
            parser.read_string("[certificate]\n" + file.read_text())
            if not parser.has_section("renewalparams"):
                raise ValueError("Missing renewal parameters")
            name = file.stem
            parser["certificate"]["archive_dir"] = str(tls / "archive" / name)
            for key in ("cert", "privkey", "chain", "fullchain"):
                parser["certificate"][key] = str(tls / "live" / name / (key + ".pem"))
            params = parser["renewalparams"]
            if params.get("authenticator") == "webroot":
                params["webroot_path"] = str(webroot) + ","
                if not parser.has_section("[webroot_map]"):
                    parser.add_section("[webroot_map]")
                for domain in parser["[webroot_map]"]:
                    parser["[webroot_map]"][domain] = str(webroot)
            output = io.StringIO()
            parser.write(output)
            text = output.getvalue().removeprefix("[certificate]\n")
        except (configparser.Error, ValueError) as exc:
            raise OpsError("Invalid Certbot renewal configuration") from exc
        temporary = file.with_name(file.name + "." + uuid4().hex)
        try:
            with os.fdopen(
                os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w"
            ) as target:
                target.write(text)
            os.replace(temporary, file)
        finally:
            temporary.unlink(missing_ok=True)


class Operations:
    def __init__(self, args):
        self.args = args
        self.root = args.root.resolve()
        env_path = args.env_file or self.root / ".env"
        if env_path.is_symlink():
            raise OpsError("Configuration symlinks are not supported")
        self.env_file = env_path.resolve()
        self.project = args.project
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,49}", self.project):
            raise OpsError("Invalid project name")
        self.directory = self.root / ".local" / "operations" / self.project
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.state_file = self.directory / "state.json"

    def command(self, *, mode=None):
        mode = mode or ("production" if self.args.production else "local")
        command = [
            "docker",
            "compose",
            "--project-name",
            self.project,
            "--env-file",
            str(self.env_file),
            "-f",
            str(self.root / "compose.yaml"),
        ]
        if os.getenv("CODEX_PROXY_CERT") or os.getenv("BUILD_CA_BUNDLE"):
            command += ["-f", str(self.root / "compose.cloud.yaml")]
        if mode != "local":
            command += ["-f", str(self.root / f"compose.{mode}.yaml")]
        return command

    def compose(self, *args, mode=None, **kwargs):
        return run(self.command(mode=mode) + list(args), **kwargs)

    def config(self):
        return json.loads(self.compose("config", "--format", "json"))

    def values(self, env_file=None):
        # Compose parses/interpolates .env; the resulting secrets stay in memory.
        command = self.command(mode="local")
        if env_file is not None:
            command[command.index("--env-file") + 1] = str(env_file)
        output = run(command + ["config", "--environment"])
        return dict(line.split("=", 1) for line in output.splitlines() if "=" in line)

    def state(self):
        return json.loads(self.state_file.read_text()) if self.state_file.exists() else {}

    def save(self, **changes):
        state = self.state()
        state.update(changes)
        atomic_json(self.state_file, state)

    def cid(self, service):
        return self.compose("ps", "--all", "--quiet", service).strip()

    def services(self):
        output = self.compose("ps", "--all", "--format", "json").strip()
        if not output:
            return []
        return (
            json.loads(output)
            if output.startswith("[")
            else [json.loads(line) for line in output.splitlines()]
        )

    def resume(self, services):
        internal = [service for service in services if service != "web"]
        for group in (internal, ["web"] if "web" in services else []):
            if not group:
                continue
            self.compose("start", *group)
            deadline = time.monotonic() + 180
            while True:
                states = {row["Service"]: row for row in self.services()}
                if all(
                    states.get(name, {}).get("State") == "running"
                    and states[name].get("Health", "") in ("", "healthy")
                    for name in group
                ):
                    break
                if time.monotonic() >= deadline:
                    raise OpsError("Timed out resuming services: " + ", ".join(group))
                time.sleep(1)

    @contextlib.contextmanager
    def quiesce(self, *, resume=True):
        running = [
            s["Service"]
            for s in self.services()
            if s["State"] == "running" and s["Service"] in WRITERS
        ]
        try:
            if running:
                self.compose("stop", "--timeout", "30", *running)
            yield running
        finally:
            if resume and running:
                self.resume(running)

    def initialize(self, settings):
        created = create_env(self.env_file)
        current = self.values()
        for key, value in settings.items():
            if value is None:
                continue
            if key in current and current[key] and current[key] != str(value):
                raise OpsError(f"{key} already configured; existing configuration preserved")
            if not current.get(key):
                append_setting(self.env_file, key, value)
        print("Configuration created" if created else "Existing configuration preserved")

    def check(self):
        output = self.compose(
            "exec",
            "-T",
            "app",
            "python",
            "-c",
            "import json,urllib.request;print(json.dumps(json.load(urllib.request.urlopen('http://127.0.0.1:8000/health/diagnostics',timeout=10))))",
            timeout=30,
        )
        data = json.loads(output)
        if data["status"] != "ok":
            raise OpsError("Application diagnostics are not healthy")
        return data

    def start(self, *, build=True):
        if build:
            self.compose("build", "app", "web")
        with self.quiesce(resume=False):
            try:
                self.compose(
                    "up",
                    "--no-build",
                    "--detach",
                    "--wait",
                    "--wait-timeout",
                    "180",
                    "db",
                    "migrate",
                    "app",
                    "worker",
                    "upload-maintenance",
                )
                self.check()
                self.compose(
                    "up",
                    "--no-deps",
                    "--no-build",
                    "--detach",
                    "--wait",
                    "--wait-timeout",
                    "180",
                    "web",
                )
            except BaseException:
                self.compose("stop", *WRITERS, check=False)
                raise
        revision = run(["git", "-C", str(self.root), "rev-parse", "HEAD"]).strip()
        dirty = bool(run(["git", "-C", str(self.root), "status", "--porcelain"]).strip())
        self.save(
            deployed_revision=revision,
            deployed_dirty=dirty,
            deployed_at=datetime.now(UTC).isoformat(),
        )
        print("Services are ready; diagnostics status=ok")

    def sql(self, statement):
        db = self.config()["services"]["db"]["environment"]
        return self.compose(
            "exec",
            "-T",
            "db",
            "psql",
            "-U",
            db["POSTGRES_USER"],
            "-d",
            db["POSTGRES_DB"],
            "-A",
            "-t",
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            statement,
        ).strip()

    def encrypt(self, work, destination, recipient, *, reserve=DEFAULT_RESERVE):
        temporary = destination.with_suffix(destination.suffix + ".part")
        budget = SpaceBudget(destination.parent, reserve)
        # Two connected processes let us enforce reserve on age's output too.
        producer = None
        try:
            with os.fdopen(
                os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb"
            ) as output:
                producer = subprocess.Popen(
                    [
                        "tar",
                        "--create",
                        "--gzip",
                        "--file=-",
                        "--directory=" + str(work),
                        "--",
                        *sorted(file.name for file in work.iterdir()),
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                )
                run(
                    ["age", "--encrypt", "--recipient", recipient],
                    stdin=producer.stdout,
                    stdout=GuardedWriter(output, budget),
                    binary=True,
                )
                producer.stdout.close()
                producer.wait(timeout=30)
                if producer.returncode:
                    raise OpsError("Snapshot compression failed")
            os.replace(temporary, destination)
        finally:
            if producer is not None:
                stop_process(producer)
                producer.stdout.close()
                producer.stderr.close()
            temporary.unlink(missing_ok=True)

    def reserve(self, values):
        return max(
            int(values.get("MEDIA_MIN_FREE_BYTES", DEFAULT_RESERVE)),
            int(values.get("BACKUP_MIN_FREE_BYTES", DEFAULT_RESERVE)),
        )

    def media_inventory(self, container):
        data = json.loads(run(["docker", "inspect", container]))[0]
        mount = next(m for m in data["Mounts"] if m["Destination"] == "/data/media")
        source = Path(mount["Source"])
        try:
            if source.is_dir() and os.access(source, os.R_OK | os.X_OK):
                return tree_inventory(source)
        except PermissionError:
            pass
        if not data["State"]["Running"]:
            raise OpsError("Inspect stopped media on the Docker host before backup")
        # The same conservative inventory when Docker's root is in another namespace.
        code = (
            "import os,json;root='/data/media';n=1;size=0;"
            "\nfor directory,names,files in os.walk(root):"
            "\n n+=len(names)+len(files)"
            "\n for name in names+files:"
            "\n  p=os.path.join(directory,name)"
            "\n  if os.path.islink(p):raise RuntimeError('Unsupported media link')"
            "\n  if os.path.isfile(p):size+=os.stat(p).st_size"
            "\nprint(size+n*8192)"
        )
        return int(self.compose("exec", "-T", "app", "python", "-c", code))

    def ship(self, archive, values=None):
        values = values or self.values()
        target = values.get("BACKUP_SSH_TARGET", "")
        directory = values.get("BACKUP_SSH_PATH", "")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*@[A-Za-z0-9][A-Za-z0-9_.-]*", target):
            raise OpsError("Configure BACKUP_SSH_TARGET=user@host")
        if (
            not directory.startswith("/")
            or ".." in PurePosixPath(directory).parts
            or any(c in directory for c in "\r\n\x00")
        ):
            raise OpsError("BACKUP_SSH_PATH must be an absolute remote directory")
        command = [
            "ssh",
            "-F",
            "/dev/null",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "ConnectTimeout=10",
            "-o",
            "ServerAliveInterval=15",
            "-o",
            "ServerAliveCountMax=3",
        ]
        if values.get("BACKUP_SSH_PORT"):
            command += ["-p", str(int(values["BACKUP_SSH_PORT"]))]
        if values.get("BACKUP_SSH_IDENTITY"):
            command += ["-i", values["BACKUP_SSH_IDENTITY"]]
        if values.get("BACKUP_SSH_KNOWN_HOSTS"):
            command += ["-o", "UserKnownHostsFile=" + values["BACKUP_SSH_KNOWN_HOSTS"]]
        remote = directory.rstrip("/") + "/" + archive.name
        temporary = remote + ".part-" + uuid4().hex
        script = (
            "set -eu; umask 077; mkdir -p -- "
            + shlex.quote(directory)
            + "; tmp="
            + shlex.quote(temporary)
            + '; trap \'rm -f -- "$tmp"\' EXIT HUP INT TERM; cat > "$tmp"; printf "%s  %s\\n" '
            + shlex.quote(digest(archive))
            + ' "$tmp" | sha256sum -c - >/dev/null; mv -- "$tmp" '
            + shlex.quote(remote)
            + "; trap - EXIT HUP INT TERM"
        )
        with archive.open("rb") as file:
            run(command + [target, script], stdin=file)
        return remote

    def backup(self, *, recipient=None, destination=None, local_only=False, resume=True, keep=None):
        values = self.values()
        keep = int(values.get("BACKUP_KEEP_LOCAL", "7")) if keep is None else keep
        if keep < 1:
            raise OpsError("BACKUP_KEEP_LOCAL must be at least 1")
        recipient = recipient or values.get("BACKUP_RECIPIENT")
        if not recipient:
            raise OpsError("Configure an age recipient before backup")
        if not shutil.which("tar"):
            raise OpsError("Install tar before creating backups")
        if not shutil.which("age"):
            raise OpsError("Install age before creating backups")
        with open(os.devnull, "rb") as empty:
            run(
                ["age", "--encrypt", "--recipient", recipient],
                stdin=empty,
                stdout=subprocess.DEVNULL,
            )
        if not local_only and not values.get("BACKUP_SSH_TARGET"):
            raise OpsError(
                "Off-site backup is not configured; --local-only is for isolated/local copies"
            )
        destination = Path(
            destination or values.get("BACKUP_DIR", self.directory / "backups")
        ).resolve()
        destination.mkdir(mode=0o700, parents=True, exist_ok=True)
        if destination.stat().st_mode & 0o077:
            raise OpsError("Backup directory must be private (0700)")
        reserve = self.reserve(values)
        budget = SpaceBudget(destination, reserve)
        container = self.cid("app")
        if not container:
            raise OpsError("Application container is needed for a consistent media snapshot")
        media_bytes = self.media_inventory(container)
        database_bytes = int(self.sql("SELECT pg_database_size(current_database())"))
        tls_bytes = 0
        if self.args.production:
            tls_bytes = sum(
                p.stat().st_size + 8192
                for p in Path(values.get("TLS_ROOT", self.directory / "letsencrypt")).rglob("*")
                if p.is_file()
            )
        estimate = media_bytes + database_bytes * 2 + tls_bytes + ARCHIVE_OVERHEAD
        # dump/media snapshot and encrypted output coexist until finally cleanup.
        budget.require(estimate * 2)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        archive = destination / f"video-{self.project}-{stamp}-{uuid4().hex[:8]}.tar.age"
        with (
            self.quiesce(resume=resume) as running,
            tempfile.TemporaryDirectory(prefix=".snapshot-", dir=destination) as folder,
        ):
            work = Path(folder)
            work.chmod(0o700)
            shutil.copyfile(self.env_file, work / "config.env")
            (work / "config.env").chmod(0o600)
            db = self.config()["services"]["db"]["environment"]
            revision = self.sql("SELECT version_num FROM alembic_version")
            with (work / "database.dump").open("wb") as file:
                self.compose(
                    "exec",
                    "-T",
                    "db",
                    "pg_dump",
                    "-U",
                    db["POSTGRES_USER"],
                    "-d",
                    db["POSTGRES_DB"],
                    "--format=custom",
                    "--no-owner",
                    "--no-acl",
                    stdout=GuardedWriter(file, budget),
                    binary=True,
                )
            container = self.cid("app")
            if not container:
                raise OpsError("Application container is needed for a consistent media snapshot")
            with (work / "media.tar").open("wb") as file:
                run(
                    ["docker", "cp", f"{container}:/data/media/.", "-"],
                    stdout=GuardedWriter(file, budget),
                    binary=True,
                )
            with tarfile.open(work / "media.tar") as tar:
                safe_members(tar)
            if self.args.production:
                tls = Path(values.get("TLS_ROOT", self.directory / "letsencrypt"))
                with (
                    (work / "tls.tar").open("wb") as output,
                    tarfile.open(fileobj=GuardedWriter(output, budget), mode="w|") as tar,
                ):
                    for child in sorted(tls.iterdir()):
                        tar.add(child, arcname=child.name)
                with tarfile.open(work / "tls.tar") as tar:
                    safe_members(tar, links=True)
            image = json.loads(run(["docker", "inspect", container]))[0]["Image"]
            files = {
                p.name: {"sha256": digest(p), "size": p.stat().st_size} for p in work.iterdir()
            }
            manifest = {
                "format": 1,
                "postgres_major": 17,
                "database_bytes": database_bytes,
                "media_bytes": media_bytes,
                "alembic_revision": revision,
                "created_at": datetime.now(UTC).isoformat(),
                "deployed_revision": self.state().get("deployed_revision"),
                "deployed_dirty": self.state().get("deployed_dirty"),
                "image": image,
                "production": self.args.production,
                "running_services": running,
                "files": files,
            }
            (work / "manifest.json").write_text(json.dumps(manifest, sort_keys=True))
            self.encrypt(work, archive, recipient, reserve=reserve)
        remote = None if local_only else self.ship(archive, values)
        self.save(
            last_backup={
                "created_at": datetime.now(UTC).isoformat(),
                "file": archive.name,
                "offsite": remote is not None,
                "sha256": digest(archive),
            }
        )
        if remote and keep > 0:
            for old in sorted(
                destination.glob(f"video-{self.project}-*.tar.age"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )[keep:]:
                old.unlink()
        print(
            json.dumps(
                {"backup": str(archive), "offsite": remote is not None, "sha256": digest(archive)}
            )
        )
        return archive

    def restore(self, archive, identity, *, http_port=None, https_port=None, public_http_port=None):
        # Refuse even stopped containers/volumes: never guess whether a target holds valuable data.
        if any(
            port is not None and not 1 <= port <= 65535
            for port in (http_port, https_port, public_http_port)
        ):
            raise OpsError("Restore ports must be between 1 and 65535")
        if self.env_file.exists():
            raise OpsError("Restore requires a new environment file and empty target")
        for command in (
            [
                "docker",
                "ps",
                "-a",
                "-q",
                "--filter",
                f"label=com.docker.compose.project={self.project}",
            ],
            [
                "docker",
                "volume",
                "ls",
                "-q",
                "--filter",
                f"label=com.docker.compose.project={self.project}",
            ],
        ):
            if run(command).strip():
                raise OpsError("Restore target already contains containers or volumes")
        reserve = DEFAULT_RESERVE
        budget = SpaceBudget(self.directory, reserve)
        budget.require(archive.stat().st_size)
        with tempfile.TemporaryDirectory(prefix="restore-", dir=self.directory) as folder:
            work = Path(folder)
            work.chmod(0o700)
            with (work / "bundle.tar").open("wb") as file:
                run(
                    ["age", "--decrypt", "--identity", str(identity), str(archive)],
                    stdout=GuardedWriter(file, budget),
                    binary=True,
                )
            payload = work / "payload"
            payload.mkdir(mode=0o700)
            manifest = unpack_bundle(work / "bundle.tar", payload, reserve=reserve)
            revisions = set()
            for file in (self.root / "migrations" / "versions").glob("*.py"):
                for node in ast.parse(file.read_text()).body:
                    if isinstance(node, ast.Assign) and any(
                        isinstance(t, ast.Name) and t.id == "revision" for t in node.targets
                    ):
                        revisions.add(ast.literal_eval(node.value))
                    if (
                        isinstance(node, ast.AnnAssign)
                        and isinstance(node.target, ast.Name)
                        and node.target.id == "revision"
                    ):
                        revisions.add(ast.literal_eval(node.value))
            if manifest["alembic_revision"] not in revisions:
                raise OpsError(
                    "Backup schema is unknown to this release; choose compatible source code"
                )
            if bool(manifest["production"]) != self.args.production:
                raise OpsError("Restore mode must match backup mode")
            reserve = self.reserve(self.values(env_file=payload / "config.env"))
            budget = SpaceBudget(self.directory, reserve)
            with tarfile.open(payload / "media.tar") as media:
                media_size = sum(member.size + 8192 for member in safe_members(media))
            db_size = manifest.get("database_bytes", (payload / "database.dump").stat().st_size * 4)
            if type(db_size) is not int or db_size < 0:
                raise OpsError("Invalid database size in snapshot")
            required = media_size + db_size * 2 + ARCHIVE_OVERHEAD
            tls_size = (payload / "tls.tar").stat().st_size if manifest["production"] else 0
            budget.require(required + tls_size)
            self.env_file.parent.mkdir(parents=True, exist_ok=True)
            with os.fdopen(
                os.open(self.env_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb"
            ) as file:
                file.write((payload / "config.env").read_bytes())
            for key, value in [
                ("HTTP_PORT", http_port),
                ("HTTPS_PORT", https_port),
                ("HTTP_PUBLIC_PORT", public_http_port),
            ]:
                if value is not None:
                    append_setting(self.env_file, key, value)
            if self.args.production:
                tls = self.directory / "letsencrypt"
                tls.mkdir(mode=0o700)
                with tarfile.open(payload / "tls.tar") as tar:
                    tar.extractall(tls, filter="data")
                acme = self.directory / "acme"
                acme.mkdir(mode=0o755)
                relocate_certbot(tls, acme)
                append_setting(self.env_file, "TLS_ROOT", str(tls))
                append_setting(self.env_file, "ACME_WEBROOT", str(acme))
            try:
                self.compose("up", "--detach", "--wait", "--wait-timeout", "180", "db")
                db = self.config()["services"]["db"]["environment"]
                budget.require(required)
                self.require_volume_space(required, reserve)

                def guard_target():
                    budget.require()
                    self.require_volume_space(0, reserve)

                with (payload / "database.dump").open("rb") as file:
                    self.compose(
                        "exec",
                        "-T",
                        "db",
                        "pg_restore",
                        "-U",
                        db["POSTGRES_USER"],
                        "-d",
                        db["POSTGRES_DB"],
                        "--exit-on-error",
                        "--single-transaction",
                        "--clean",
                        "--if-exists",
                        "--no-owner",
                        "--no-acl",
                        stdin=GuardedReader(file, guard_target),
                        binary=True,
                    )
                self.compose("create", "--no-build", "app")
                with (payload / "media.tar").open("rb") as file:
                    run(
                        ["docker", "cp", "-a", "-", f"{self.cid('app')}:/data/media"],
                        stdin=GuardedReader(file, guard_target),
                        binary=True,
                    )
                self.start(build=False)
            except BaseException:
                # Killing docker exec's client does not guarantee remote pg_restore exits.
                # Stop the new DB as well so an import cannot keep writing after reserve failure.
                self.compose("stop", "--timeout", "30", *WRITERS, "db", check=False)
                raise
        print("Restore completed; database, media and private grants are available")

    def require_volume_space(self, required, reserve):
        output = self.compose(
            "exec", "-T", "db", "df", "-P", "-B1", "/var/lib/postgresql/data", timeout=30
        )
        free = int(output.splitlines()[-1].split()[3])
        if free < required + reserve:
            raise OpsError(
                f"Insufficient Docker volume space: need {required} bytes plus "
                f"{reserve} reserve; available {free}"
            )

    def web_mode(self):
        container = self.cid("web")
        if not container:
            return None
        data = json.loads(run(["docker", "inspect", container]))[0]
        if not data["State"]["Running"]:
            return None
        source = next(
            (
                m["Source"]
                for m in data["Mounts"]
                if m["Destination"] == "/etc/nginx/templates/default.conf.template"
            ),
            "",
        )
        for mode, name in (
            ("production", "nginx.production.conf.template"),
            ("acme", "nginx.acme.conf.template"),
            ("local", "nginx.conf.template"),
        ):
            if source == str(self.root / "deploy" / name):
                return mode
        raise OpsError("Unknown running web configuration; refusing to replace it")

    def tls(self, *, renew=False):
        values = self.values()
        domain = values.get("SITE_DOMAIN", "")
        email = values.get("ACME_EMAIL", "")
        if (
            not re.fullmatch(r"(?=.{1,253}$)[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", domain)
            or "." not in domain
            or ".." in domain
        ):
            raise OpsError("Configure a valid SITE_DOMAIN")
        if not re.fullmatch(r"[^\s@]+@[^\s@]+", email):
            raise OpsError("Configure ACME_EMAIL")
        tls = Path(values.get("TLS_ROOT", self.directory / "letsencrypt"))
        webroot = Path(values.get("ACME_WEBROOT", self.directory / "acme"))
        tls.mkdir(parents=True, exist_ok=True, mode=0o700)
        webroot.mkdir(parents=True, exist_ok=True, mode=0o755)
        webroot.chmod(0o755)
        if not values.get("TLS_ROOT"):
            append_setting(self.env_file, "TLS_ROOT", str(tls))
        if not values.get("ACME_WEBROOT"):
            append_setting(self.env_file, "ACME_WEBROOT", str(webroot))
        certificate = tls / "live" / domain / "fullchain.pem"
        if not renew and certificate.exists():
            check = subprocess.run(
                ["openssl", "x509", "-in", str(certificate), "-noout", "-checkend", "86400"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
            )
            if check.returncode == 0:
                return
        previous_mode = None
        bootstrapped = False
        if not renew:
            previous_mode = self.web_mode()
            if previous_mode not in ("production", "acme"):
                self.compose("build", "web", mode="acme")
                try:
                    self.compose(
                        "up",
                        "--no-build",
                        "--no-deps",
                        "--detach",
                        "--wait",
                        "--wait-timeout",
                        "60",
                        "web",
                        mode="acme",
                    )
                except BaseException:
                    if previous_mode:
                        self.compose(
                            "up",
                            "--no-deps",
                            "--no-build",
                            "--detach",
                            "--wait",
                            "--wait-timeout",
                            "180",
                            "web",
                            mode=previous_mode,
                        )
                    raise
                bootstrapped = True
        args = [
            "certbot",
            "renew" if renew else "certonly",
            "--non-interactive",
            "--config-dir",
            str(tls),
            "--work-dir",
            str(self.directory / "certbot-work"),
            "--logs-dir",
            str(self.directory / "certbot-log"),
            "--webroot",
            "--webroot-path",
            str(webroot),
            "--cert-name",
            domain,
        ]
        if not renew:
            args += ["--agree-tos", "--email", email, "-d", domain, "--keep-until-expiring"]
        try:
            run(args)
        except BaseException:
            if bootstrapped and previous_mode:
                self.compose(
                    "up",
                    "--no-deps",
                    "--no-build",
                    "--detach",
                    "--wait",
                    "--wait-timeout",
                    "180",
                    "web",
                    mode=previous_mode,
                )
            raise
        if renew or previous_mode == "production":
            self.compose("exec", "-T", "web", "nginx", "-t")
            self.compose("exec", "-T", "web", "nginx", "-s", "reload")
        print("TLS configuration ready")

    def status(self):
        result = {
            "services": [
                {k: s.get(k) for k in ("Service", "State", "Health", "ExitCode")}
                for s in self.services()
            ],
            "backup": self.state().get("last_backup"),
        }
        try:
            result["diagnostics"] = json.loads(
                self.compose(
                    "exec",
                    "-T",
                    "app",
                    "python",
                    "-c",
                    "import json,urllib.request;print(json.dumps(json.load(urllib.request.urlopen('http://127.0.0.1:8000/health/diagnostics',timeout=10))))",
                    timeout=30,
                )
            )
            result["resources"] = json.loads(
                self.compose("exec", "-T", "app", "python", "-m", "app.operations", timeout=30)
            )
        except (OpsError, json.JSONDecodeError):
            result["diagnostics"] = {"status": "unavailable"}
        errors = {}
        for line in self.compose(
            "logs", "--no-color", "--tail", "300", "web", check=False
        ).splitlines():
            try:
                entry = json.loads(line[line.index("{") :])
            except (ValueError, json.JSONDecodeError):
                continue
            if isinstance(entry.get("status"), int) and entry["status"] >= 400:
                key = str(entry["status"])
                errors[key] = errors.get(key, 0) + 1
        result["recent_http_errors"] = errors
        values = self.values()
        result["backup_configured"] = all(
            values.get(key) for key in ("BACKUP_RECIPIENT", "BACKUP_SSH_TARGET", "BACKUP_SSH_PATH")
        )
        service_states = {row["Service"]: row for row in result["services"]}
        healthy = result["diagnostics"]["status"] == "ok" and all(
            service_states.get(name, {}).get("State") == "running"
            and service_states[name].get("Health") in (None, "", "healthy")
            for name in (*WRITERS, "db")
        )
        healthy = healthy and result.get("resources", {}).get("database") != "unavailable"
        if self.args.production:
            certificate = (
                Path(values.get("TLS_ROOT", self.directory / "letsencrypt"))
                / "live"
                / values.get("SITE_DOMAIN", "")
                / "fullchain.pem"
            )
            try:
                expiry = (
                    run(["openssl", "x509", "-in", str(certificate), "-noout", "-enddate"])
                    .strip()
                    .split("=", 1)[1]
                )
                remaining = datetime.strptime(expiry, "%b %d %H:%M:%S %Y %Z").replace(
                    tzinfo=UTC
                ) - datetime.now(UTC)
                result["tls"] = {"expires_at": expiry, "days_remaining": remaining.days}
                healthy = healthy and remaining.days >= 14
            except (OpsError, ValueError, IndexError):
                result["tls"] = {"status": "unavailable"}
                healthy = False
        result["status"] = "ok" if healthy else "degraded"
        print(json.dumps(result, sort_keys=True))
        return 0 if healthy else 1

    def update(self, ref, *, recipient=None, destination=None, local_only=False):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}", ref):
            raise OpsError("Use a commit, tag or branch name")
        if run(["git", "-C", str(self.root), "status", "--porcelain"]).strip():
            raise OpsError("Update requires a clean checkout")
        previous = run(["git", "-C", str(self.root), "rev-parse", "HEAD"]).strip()
        run(["git", "-C", str(self.root), "fetch", "origin", ref])
        target = run(["git", "-C", str(self.root), "rev-parse", "FETCH_HEAD"]).strip()
        if target == previous:
            print("Already at requested release")
            return
        running = [
            s["Service"]
            for s in self.services()
            if s["State"] == "running" and s["Service"] in WRITERS
        ]
        images = {}
        for service in ("app", "web"):
            container = self.cid(service)
            if container:
                data = json.loads(run(["docker", "inspect", container]))[0]
                images[data["Config"]["Image"]] = data["Image"]
        migrated = False
        archive = None
        try:
            archive = self.backup(
                recipient=recipient, destination=destination, local_only=local_only, resume=False
            )
            run(["git", "-C", str(self.root), "checkout", "--detach", target])
            self.compose("build", "app", "web")
            migrated = True
            self.start(build=False)
        except BaseException:
            if not migrated:
                run(["git", "-C", str(self.root), "checkout", "--detach", previous])
                for tag, image in images.items():
                    run(["docker", "image", "tag", image, tag])
                if running:
                    self.resume(running)
            else:
                self.compose("stop", *WRITERS, check=False)
                print(
                    f"Upgrade stopped safely. Backup: {archive}. Previous revision: {previous}. "
                    "Restore into an empty target or repair forward.",
                    file=sys.stderr,
                )
            raise


def parser():
    p = argparse.ArgumentParser(description="Video platform operator tools")
    p.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    p.add_argument("--env-file", type=Path)
    p.add_argument("--project", default="video-platform")
    p.add_argument("--production", action="store_true")
    commands = p.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init")
    for option in ("domain", "email", "recipient", "ssh-target", "ssh-path"):
        init.add_argument("--" + option)
    start = commands.add_parser("start")
    start.add_argument("--no-build", action="store_true")
    for name in ("status", "tls", "renew", "configuration"):
        commands.add_parser(name)
    ship = commands.add_parser("ship")
    ship.add_argument("archive", type=Path)
    for name in ("backup", "update"):
        command = commands.add_parser(name)
        if name == "update":
            command.add_argument("ref")
        command.add_argument("--recipient")
        command.add_argument("--destination", type=Path)
        command.add_argument("--local-only", action="store_true")
    restore = commands.add_parser("restore")
    restore.add_argument("archive", type=Path)
    restore.add_argument("--identity", type=Path, required=True)
    for option in ("http-port", "https-port", "public-http-port"):
        restore.add_argument("--" + option, type=int)
    return p


def main():
    args = parser().parse_args()
    ops = Operations(args)

    def interrupted(*_):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    try:
        with (ops.directory / "operation.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if args.command == "init":
                ops.initialize(
                    {
                        "SITE_DOMAIN": args.domain,
                        "ACME_EMAIL": args.email,
                        "BACKUP_RECIPIENT": args.recipient,
                        "BACKUP_SSH_TARGET": args.ssh_target,
                        "BACKUP_SSH_PATH": args.ssh_path,
                    }
                )
            elif args.command == "start":
                create_env(ops.env_file)
                ops.start(build=not args.no_build)
            elif args.command == "configuration":
                values = ops.values()
                print(
                    json.dumps(
                        {
                            "production": bool(values.get("SITE_DOMAIN")),
                            "backup_configured": all(
                                values.get(key)
                                for key in (
                                    "BACKUP_RECIPIENT",
                                    "BACKUP_SSH_TARGET",
                                    "BACKUP_SSH_PATH",
                                )
                            ),
                        }
                    )
                )
            elif args.command == "status":
                return ops.status()
            elif args.command == "backup":
                ops.backup(
                    recipient=args.recipient,
                    destination=args.destination,
                    local_only=args.local_only,
                )
            elif args.command == "update":
                ops.update(
                    args.ref,
                    recipient=args.recipient,
                    destination=args.destination,
                    local_only=args.local_only,
                )
            elif args.command == "restore":
                ops.restore(
                    args.archive,
                    args.identity,
                    http_port=args.http_port,
                    https_port=args.https_port,
                    public_http_port=args.public_http_port,
                )
            elif args.command == "ship":
                print(json.dumps({"remote": ops.ship(args.archive)}))
            elif args.command in ("tls", "renew"):
                ops.tls(renew=args.command == "renew")
    except BlockingIOError:
        raise OpsError("Another operator operation is already running") from None
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OpsError, ValueError, OSError, subprocess.TimeoutExpired) as exc:
        print(f"Operation failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
