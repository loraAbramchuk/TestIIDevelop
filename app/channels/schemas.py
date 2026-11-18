from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.json_schema import SkipJsonSchema


def omit_default(schema):
    schema.pop("default", None)


class ChannelSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    owner_id: UUID
    name: str


class ChannelView(ChannelSummary):
    description: str
    created_at: datetime
    updated_at: datetime
    subscriber_count: int = Field(ge=0)
    is_subscribed: bool


class ChannelPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | SkipJsonSchema[None] = Field(
        default=None, min_length=1, max_length=100, json_schema_extra=omit_default
    )
    description: str | SkipJsonSchema[None] = Field(
        default=None, max_length=2000, json_schema_extra=omit_default
    )

    @field_validator("name", "description", mode="before")
    @classmethod
    def validate_text(cls, value, info):
        if value is None:
            raise ValueError("Omit unchanged fields instead of null")
        if isinstance(value, str):
            if "\x00" in value:
                raise ValueError("NUL is not allowed")
            if info.field_name == "name":
                return value.strip()
        return value


class SubscriptionState(BaseModel):
    channel_id: UUID
    is_subscribed: bool
    subscriber_count: int = Field(ge=0)


class ChannelList(BaseModel):
    items: list[ChannelView]
    next_cursor: str | None = None
