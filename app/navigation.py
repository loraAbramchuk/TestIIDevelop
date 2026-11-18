"""Local destinations for returning to a page after authentication."""

from urllib.parse import unquote, urlencode, urlsplit

from fastapi.responses import RedirectResponse


def local_return_path(value):
    if not value or not value.startswith("/") or len(value) > 2048:
        return "/"
    try:
        decoded = unquote(value, errors="strict")
        if (
            not decoded.startswith("/")
            or decoded.startswith("//")
            or "\\" in decoded
            or any(ord(char) < 32 or ord(char) == 127 for char in decoded)
        ):
            return "/"
        decoded.encode("utf-8")
        parsed = urlsplit(value)
        if parsed.scheme or parsed.netloc:
            return "/"
    except (ValueError, UnicodeError):
        return "/"
    return value


def login_redirect(request):
    return RedirectResponse("/login?" + urlencode({"next": request.url.path}), status_code=303)
