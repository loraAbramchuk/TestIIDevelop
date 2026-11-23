from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.channels.schemas import ChannelSummary
from app.text_validation import validate_text


class LikeState(BaseModel):
    video_id: UUID
    like_count: int = Field(ge=0)
    is_liked: bool


class CommentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=2000)

    @field_validator("text", mode="before")
    @classmethod
    def clean_text(cls, value):
        value = validate_text(value)
        return value.strip() if isinstance(value, str) else value


class CommentView(BaseModel):
    id: UUID
    video_id: UUID
    text: str
    created_at: datetime
    author: ChannelSummary
    can_delete: bool


class CommentPage(BaseModel):
    items: list[CommentView]
    next_cursor: str | None = None
    total_count: int = Field(ge=0)
