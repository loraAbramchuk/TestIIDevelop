from app.api import health


def test_home_and_static(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "Пока нет видео" in response.text
    assert client.get("/static/styles.css").status_code == 200


def test_docs_and_openapi(client):
    assert client.get("/docs").status_code == 200
    schema = client.get("/openapi.json").json()
    assert "/health/ready" in schema["paths"]
    assert "503" in schema["paths"]["/health/ready"]["get"]["responses"]


def test_liveness_does_not_require_database(client):
    assert client.get("/health/live").json() == {"status": "ok"}


def test_readiness_rejects_unavailable_database(client):
    response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json() == {
        "status": "unavailable",
        "checks": {"database": "error", "storage": "ok"},
    }
    assert response.headers["cache-control"] == "no-store"


def test_readiness_checks_storage_even_if_database_fails(client, monkeypatch):
    def fail(root):
        raise OSError("disk full")

    monkeypatch.setattr(health, "check_storage", fail)
    response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json()["checks"] == {"database": "error", "storage": "error"}


def test_storage_probe_is_removed(client, app):
    client.get("/health/ready")
    assert list(app.state.settings.media_root.iterdir()) == []
