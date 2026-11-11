import hashlib
import hmac
import secrets
from threading import BoundedSemaphore

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

password_hasher = PasswordHasher()
_hash_slots = BoundedSemaphore(2)
# Unknown accounts still perform Argon2 verification to avoid a cheap timing distinction.
_dummy_hash = password_hasher.hash(secrets.token_urlsafe(32))


def hash_password(password: str) -> str:
    # Argon2 uses ~64 MiB per job; cap concurrent jobs in each HTTP worker.
    with _hash_slots:
        return password_hasher.hash(password)


def verify_password(password: str, stored_hash: str | None) -> bool:
    try:
        with _hash_slots:
            return password_hasher.verify(stored_hash or _dummy_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def csrf_token(session_token: str) -> str:
    # The secret is the random HttpOnly session cookie; no global signing secret is needed.
    return hmac.new(session_token.encode(), b"video-platform:csrf:v1", hashlib.sha256).hexdigest()
