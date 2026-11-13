import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from openapi_spec_validator import validate
from pydantic import ValidationError

from app.config import Settings
from app.video_contracts import VideoCreate, VideoPatch


def settings(**overrides):
    return Settings(_env_file=None, postgres_password="test-password-not-production", **overrides)


def test_contract_openapi_marks_only_implemented_operations(client):
    document = json.loads(Path("docs/api/video.openapi.json").read_text())
    validate(document)
    actual_paths = client.get("/openapi.json").json()["paths"]
    for path, path_item in document["paths"].items():
        for method, operation in path_item.items():
            if method in ("get", "post", "put", "patch", "delete", "head"):
                assert operation["x-implemented"] == (method in actual_paths.get(path, {}))


def test_create_metadata_default_private_and_owner_cannot_be_supplied():
    value = VideoCreate(title="  Название  ")
    assert value.title == "Название" and value.visibility == "private"
    for payload in (
        {"title": " "},
        {"title": "Valid", "owner_id": "forged"},
        {"title": "Valid", "processing_status": "ready"},
    ):
        with pytest.raises(ValidationError):
            VideoCreate.model_validate(payload)


def test_patch_omission_differs_from_null_and_status_is_protected():
    assert VideoPatch().model_dump(exclude_unset=True) == {}
    assert VideoPatch(title=" Test ").title == "Test"
    for value in (
        {"title": None},
        {"description": None},
        {"visibility": None},
        {"processing_status": "ready"},
        {"title": " "},
    ):
        with pytest.raises(ValidationError):
            VideoPatch.model_validate(value)


def test_generated_patch_schema_rejects_explicit_null():
    schema = VideoPatch.model_json_schema()
    validator = Draft202012Validator(schema)
    assert validator.is_valid({})
    assert validator.is_valid({"title": "Changed", "visibility": "private"})
    for field in ("title", "description", "visibility"):
        assert not validator.is_valid({field: None})


def test_upload_reservation_and_invalid_limit_combinations():
    config = settings()
    assert config.max_upload_bytes == 1024**3
    assert config.upload_reservation_bytes(100) == 200 + 16 * 1024**2
    assert config.upload_reservation_bytes() == 2 * 1024**3 + 16 * 1024**2
    for size in (0, -1, config.max_upload_bytes + 1):
        with pytest.raises(ValueError):
            config.upload_reservation_bytes(size)
    for overrides in (
        {"owner_media_quota_bytes": 1024},
        {"max_uploads_total": 1},
        {"upload_lease_seconds": 20},
        {"upload_timeout_seconds": 10},
        {"media_capacity_bytes": 1024},
    ):
        with pytest.raises(ValidationError):
            settings(**overrides)
