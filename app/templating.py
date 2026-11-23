import hashlib
from functools import lru_cache
from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates

PACKAGE_ROOT = Path(__file__).resolve().parent


@lru_cache(maxsize=256)
def _asset_digest(path: Path, modified: int, size: int) -> str:
    # The stat values invalidate the cache when a development asset changes.
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def static_url(request: Request, path: str) -> str:
    asset = PACKAGE_ROOT / "static" / path.lstrip("/")
    info = asset.stat()
    revision = _asset_digest(asset, info.st_mtime_ns, info.st_size)
    # Keep the reverse proxy's root_path and stay on the current origin.
    return f"{request.url_for('static', path=path).path}?v={revision}"


templates = Jinja2Templates(directory=PACKAGE_ROOT / "templates")
templates.env.globals["static_url"] = static_url
