"""Create configuration once; never print or overwrite existing credentials."""

import argparse
import os
import secrets
import tempfile
from pathlib import Path


def create_env(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if path.is_symlink() or not path.is_file():
            raise ValueError("Configuration must be a regular file") from None
        return False
    with os.fdopen(fd, "w") as file:
        file.write(f"POSTGRES_PASSWORD={secrets.token_hex(32)}\n")
        file.write("POSTGRES_USER=video\nPOSTGRES_DB=video\nHTTP_PORT=8080\n")
        file.write(f"AUTH_MAIL_SECRET={secrets.token_hex(32)}\nAUTH_REQUIRE_VERIFIED_EMAIL=true\n")
    return True


def prepare_mail(path):
    """Add a signing secret to an existing config without printing/replacing credentials."""
    import fcntl
    import stat

    path = Path(path)
    lock_fd = os.open(
        path.with_name(path.name + ".mail.lock"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
    )
    temporary = None
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd) as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise ValueError("Configuration must be a regular file")
            content = source.read()
        for line in reversed(content.splitlines()):
            name, sep, value = line.removeprefix("export ").partition("=")
            if sep and name.strip() == "AUTH_MAIL_SECRET":
                if value.strip().strip("\"'"):
                    return False
                break
        with tempfile.NamedTemporaryFile(
            mode="w", prefix=".mail-env-", dir=path.parent, delete=False
        ) as target:
            temporary = Path(target.name)
            target.write(content.rstrip("\n") + f"\nAUTH_MAIL_SECRET={secrets.token_hex(32)}\n")
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
        return True
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)
        os.close(lock_fd)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--path", type=Path, default=Path(__file__).resolve().parent.parent / ".env"
    )
    parser.add_argument(
        "--prepare-mail",
        action="store_true",
        help="Add a missing mail signing secret, preserving other settings",
    )
    args = parser.parse_args()
    if args.prepare_mail:
        print(
            "Mail secret prepared" if prepare_mail(args.path) else "Existing mail secret preserved"
        )
    else:
        print(
            "Created .env with a generated database password"
            if create_env(args.path)
            else "Existing .env preserved"
        )
