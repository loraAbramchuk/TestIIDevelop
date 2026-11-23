"""Shared local mail selection; also usable by the standard-library-only launcher."""

from urllib.parse import urlsplit

LOOPBACK = {"localhost", "127.0.0.1", "::1"}


def uses_mailpit(backend, smtp_host, secure, public_base_url):
    if backend not in ("auto", "smtp", "mailpit"):
        raise ValueError("MAIL_BACKEND must be auto, smtp or mailpit")
    if backend == "smtp" or backend == "auto" and smtp_host:
        return False
    origin = urlsplit(public_base_url)
    return not secure and origin.scheme == "http" and origin.hostname in LOOPBACK
