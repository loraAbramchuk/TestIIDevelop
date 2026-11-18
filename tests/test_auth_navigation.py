from html.parser import HTMLParser
from types import SimpleNamespace
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

from app.auth_dependencies import optional_user


class AuthMarkup(HTMLParser):
    def __init__(self, content):
        super().__init__()
        self.destination = None
        self.links = []
        self.feed(content)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form" and attrs.get("id") == "auth-form":
            self.destination = attrs.get("data-next")
        if tag == "a":
            self.links.append(attrs.get("href", ""))


@pytest.mark.parametrize("path", ["/login", "/register"])
@pytest.mark.parametrize(
    "target,expected",
    [
        ("/channels/29a40cd9-3827-4d05-b21b-d52cf09c19e2", None),
        ('/search?q=тест&title="название"', None),
        ("https://example.com", "/"),
        ("//example.com", "/"),
        ("/\\example.com", "/"),
        ("/%2fexample.com", "/"),
        ("/%5cexample.com", "/"),
        ("/\n/example.com", "/"),
        ("/%0d%0aLocation:https://example.com", "/"),
        ("/bad%ff", "/"),
        ("/" + "x" * 2048, "/"),
    ],
)
def test_auth_pages_preserve_only_local_return_destinations(app, client, path, target, expected):
    expected = target if expected is None else expected
    app.dependency_overrides[optional_user] = lambda: None
    response = client.get(path, params={"next": target})
    assert response.status_code == 200
    markup = AuthMarkup(response.text)
    assert markup.destination == expected
    other = "/register" if path == "/login" else "/login"
    switch = next(link for link in markup.links if urlsplit(link).path == other)
    assert parse_qs(urlsplit(switch).query)["next"] == [expected]
    app.dependency_overrides[optional_user] = lambda: SimpleNamespace(id="authenticated")
    response = client.get(path, params={"next": target}, follow_redirects=False)
    assert response.status_code == 303
    # RedirectResponse percent-encodes non-ASCII and quote characters.
    assert unquote(response.headers["location"]) == expected


@pytest.mark.parametrize("path", ["/my-channel", "/subscriptions", "/studio", "/upload"])
def test_protected_pages_keep_the_destination_for_login(app, client, path):
    app.dependency_overrides[optional_user] = lambda: None
    response = client.get(path, follow_redirects=False)
    assert response.status_code == 303
    destination = urlsplit(response.headers["location"])
    assert destination.path == "/login" and parse_qs(destination.query)["next"] == [path]
