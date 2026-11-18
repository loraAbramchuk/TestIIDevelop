from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.catalog.schemas import CatalogVideo
from app.channels.schemas import ChannelSummary
from app.text_validation import validate_text


class PlaylistTitle(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=200)

    @field_validator("title", mode="before")
    @classmethod
    def clean_title(cls, value):
        value = validate_text(value)
        return value.strip() if isinstance(value, str) else value


class PlaylistMove(BaseModel):
    model_config = ConfigDict(extra="forbid")
    before_video_id: UUID | None = None


class PlaylistView(BaseModel):
    id: UUID
    title: str
    author: ChannelSummary
    visible_count: int = Field(ge=0)
    can_manage: bool
    created_at: datetime
    updated_at: datetime


class PlaylistList(BaseModel):
    items: list[PlaylistView]
    next_cursor: str | None = None


class PlaylistVideos(BaseModel):
    items: list[CatalogVideo]
    next_cursor: str | None = None
    visible_count: int = Field(ge=0)
