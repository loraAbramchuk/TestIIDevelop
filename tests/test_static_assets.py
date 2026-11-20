import hashlib
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient


class Resources(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.urls = []
        self.feed(html)

    def handle_starttag(self, tag, attributes):
        attributes = dict(attributes)
        if tag in ("script", "link"):
            url = attributes.get("src") or attributes.get("href")
            if url:
                self.urls.append(url)


@pytest.mark.parametrize("prefix", ["", "/video"])
@pytest.mark.parametrize("page", ["/login", "/register", "/docs"])
def test_browser_assets_match_html_and_revalidate_after_upgrades(app, prefix, page):
    with TestClient(app, root_path=prefix) as client:
        response = client.get(page)
        assert response.status_code == 200
        urls = Resources(response.text).urls
        assert len(urls) >= 3
        for url in urls:
            parsed = urlsplit(url)
            assert not parsed.scheme and not parsed.netloc
            assert parsed.path.startswith(prefix + "/static/")
            asset = client.get(url)
            assert asset.status_code == 200
            assert parse_qs(parsed.query)["v"] == [hashlib.sha256(asset.content).hexdigest()[:16]]
            assert asset.headers["cache-control"] == "no-cache"
            cached = client.get(url, headers={"If-None-Match": asset.headers["etag"]})
            assert cached.status_code == 304
            assert cached.headers["cache-control"] == "no-cache"


def test_changed_css_gets_a_new_url_in_the_same_process(app, monkeypatch, tmp_path):
    from app import application, templating

    root = tmp_path / "assets"
    static = root / "static"
    static.mkdir(parents=True)
    for name in ("styles.css", "favicon.svg", "navigation.js", "auth.js", "api.js", "auth-mail.js"):
        (static / name).write_bytes((templating.PACKAGE_ROOT / "static" / name).read_bytes())
    monkeypatch.setattr(application, "PACKAGE_ROOT", root)
    monkeypatch.setattr(templating, "PACKAGE_ROOT", root)
    css = static / "styles.css"

    def stylesheet(client):
        return next(url for url in Resources(client.get("/login").text).urls if "styles.css" in url)

    with TestClient(application.create_app(app.state.settings)) as client:
        before = stylesheet(client)
        css.write_text("body { color: red; }")
        after = stylesheet(client)
        assert before != after
        assert client.get(after).text == "body { color: red; }"
        # A copy/rebuild with identical bytes keeps the URL stable.
        css.write_text("body { color: red; }")
        assert stylesheet(client) == after


def test_legacy_unversioned_static_url_requires_revalidation(client):
    response = client.get("/static/styles.css")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache"
