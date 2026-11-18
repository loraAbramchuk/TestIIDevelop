from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.text_validation import validate_text

Role = Literal["user", "moderator", "admin"]
RoleFilter = Literal["all", "user", "moderator", "admin"]


class AdminUser(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    email: str
    display_name: str
    role: Role
    created_at: datetime


class UserPage(BaseModel):
    items: list[AdminUser]
    admin_count: int
    next_cursor: str | None = None


class RoleChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Role
    reason: str = Field(min_length=1, max_length=2000)
    expected_role: Role | None = None

    @field_validator("reason", mode="before")
    @classmethod
    def clean_reason(cls, value):
        value = validate_text(value)
        return value.strip() if isinstance(value, str) else value


class UserProfile(BaseModel):
    user: AdminUser
    admin_count: int


class RoleChangeResult(UserProfile):
    changed: bool
    actor_role: Role
