from app.api import health
from app.storage_health import MARKER


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

    monkeypatch.setattr(health, "check_storage_read", fail)
    response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json()["checks"] == {"database": "error", "storage": "error"}


def test_storage_probe_is_removed(client, app):
    client.get("/health/diagnostics")
    assert [path.name for path in app.state.settings.media_root.iterdir()] == [MARKER]


def test_readonly_storage_does_not_block_readiness(client, app, monkeypatch):
    monkeypatch.setattr(health, "check_database", lambda request: None)
    root = app.state.settings.media_root
    root.chmod(0o500)
    try:
        assert client.get("/health/ready").status_code == 200
        response = client.get("/health/diagnostics")
        assert response.status_code == 200
        assert response.json()["status"] == "degraded"
        assert response.json()["checks"]["storage_write"] == "error"
        assert response.json()["checks"]["processing"] == "not_configured"
        assert response.json()["video_uploads"] == "not_implemented"
    finally:
        root.chmod(0o700)


def test_low_free_space_does_not_block_viewers(client, app, monkeypatch):
    monkeypatch.setattr(health, "check_database", lambda request: None)
    app.state.settings.media_min_free_bytes = 2**63
    assert client.get("/health/ready").status_code == 200
    result = client.get("/health/diagnostics").json()
    assert result["status"] == "degraded"
    assert result["checks"]["disk_reserve"] == "low"


def test_missing_or_corrupt_read_marker_is_unready(client, app, monkeypatch):
    monkeypatch.setattr(health, "check_database", lambda request: None)
    marker = app.state.settings.media_root / MARKER
    marker.write_bytes(b"bad marker")
    assert client.get("/health/ready").status_code == 503
    marker.unlink()
    assert client.get("/health/ready").status_code == 503
