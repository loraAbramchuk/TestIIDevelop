"""Create configuration once; never print or overwrite existing credentials."""

import argparse
import os
import secrets
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
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--path", type=Path, default=Path(__file__).resolve().parent.parent / ".env"
    )
    args = parser.parse_args()
    print(
        "Created .env with a generated database password"
        if create_env(args.path)
        else "Existing .env preserved"
    )
