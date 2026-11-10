"""Create a local configuration once, without printing or overwriting credentials."""

import os
import secrets
from pathlib import Path

root = Path(__file__).resolve().parent.parent
path = root / ".env"
try:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
except FileExistsError:
    print("Existing .env preserved")
else:
    with os.fdopen(fd, "w") as file:
        file.write(f"POSTGRES_PASSWORD={secrets.token_hex(32)}\n")
        file.write("POSTGRES_USER=video\nPOSTGRES_DB=video\nHTTP_PORT=8080\n")
    print("Created .env with a generated database password")
