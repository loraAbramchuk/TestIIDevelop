"""Bounded keyset cursors, scoped to the query and viewer; never an access grant."""

import base64
import binascii
import json
from datetime import datetime
from uuid import UUID

from app.errors import DomainError


def encode_cursor(scope: str, stamp: datetime, identifier: UUID) -> str:
    raw = json.dumps([scope, stamp.isoformat(), str(identifier)], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(value: str | None, scope: str):
    if value is None:
        return None
    try:
        if len(value) > 1024:
            raise ValueError
        parts = json.loads(
            base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        )
        if not isinstance(parts, list) or len(parts) != 3 or parts[0] != scope:
            raise ValueError
        if not all(isinstance(part, str) for part in parts):
            raise ValueError
        stamp, identifier = datetime.fromisoformat(parts[1]), UUID(parts[2])
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError
        return stamp, identifier
    except (ValueError, TypeError, binascii.Error, UnicodeDecodeError, OverflowError):
        raise DomainError(400, "invalid_cursor", "Некорректный курсор страницы") from None
