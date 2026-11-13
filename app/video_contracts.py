"""Video contracts. Metadata/ACL are implemented in stage 2; media routes remain planned."""

from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.json_schema import SkipJsonSchema


def omit_default(schema):
    schema.pop("default", None)


class Visibility(StrEnum):
    PUBLIC = "public"
    PRIVATE = "private"


class ProcessingStatus(StrEnum):
    UPLOADING = "uploading"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"


class ModerationStatus(StrEnum):
    VISIBLE = "visible"
    HIDDEN = "hidden"


class VideoCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=5000)
    visibility: Visibility = Visibility.PRIVATE

    @field_validator("title", mode="before")
    @classmethod
    def trim_title(cls, value):
        return value.strip() if isinstance(value, str) else value


class VideoPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # All supplied fields are validated with the same constraints as create.
    title: str | SkipJsonSchema[None] = Field(
        default=None, min_length=1, max_length=200, json_schema_extra=omit_default
    )
    description: str | SkipJsonSchema[None] = Field(
        default=None, max_length=5000, json_schema_extra=omit_default
    )
    visibility: Visibility | SkipJsonSchema[None] = Field(
        default=None, json_schema_extra=omit_default
    )

    @field_validator("title", "description", "visibility", mode="before")
    @classmethod
    def reject_explicit_null(cls, value):
        if value is None:
            raise ValueError("Omit an unchanged field instead of sending null")
        return value

    @field_validator("title", mode="before")
    @classmethod
    def trim_title(cls, value):
        return value.strip() if isinstance(value, str) else value


class VideoOwnerStatus(BaseModel):
    id: UUID
    owner_id: UUID
    title: str
    description: str
    visibility: Visibility
    processing_status: ProcessingStatus
    moderation_status: ModerationStatus
    error_code: str | None = None
    moderation_reason: str | None = None
    created_at: datetime
    updated_at: datetime


class VideoView(BaseModel):
    id: UUID
    owner_id: UUID
    title: str
    description: str
    visibility: Visibility
    duration_seconds: float = Field(gt=0, allow_inf_nan=False)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    stream_url: str
    thumbnail_url: str
    created_at: datetime


class UploadAccepted(BaseModel):
    id: UUID
    processing_status: Literal["processing"] = "processing"
    status_url: str


class OwnedVideoList(BaseModel):
    items: list[VideoOwnerStatus]
    next_cursor: str | None = None


class VideoGrant(BaseModel):
    user_id: UUID
    created_at: datetime


class GrantList(BaseModel):
    items: list[VideoGrant]
    next_cursor: str | None = None


class ContractError(BaseModel):
    detail: str
    code: str | None = None
