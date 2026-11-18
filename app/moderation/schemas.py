from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.audit.schemas import AuditPage as AuditPage
from app.audit.schemas import AuditView as AuditView
from app.text_validation import validate_text

ReportReason = Literal["spam", "prohibited", "copyright", "other"]
ReportStatus = Literal["open", "resolved", "dismissed"]
ReportFilter = Literal["open", "resolved", "dismissed", "all"]


class ReportCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: ReportReason
    explanation: str = Field(default="", max_length=2000)

    @field_validator("explanation", mode="before")
    @classmethod
    def clean_text(cls, value):
        return validate_text(value)


class ReasonedAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=2000)

    @field_validator("reason", mode="before")
    @classmethod
    def clean_reason(cls, value):
        value = validate_text(value)
        return value.strip() if isinstance(value, str) else value


class ReportDecision(ReasonedAction):
    status: Literal["resolved", "dismissed"]


class VideoModeration(ReasonedAction):
    status: Literal["hidden", "visible"]


class ReportReceipt(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    video_id: UUID
    reason: ReportReason
    explanation: str
    status: ReportStatus
    created_at: datetime


class ModerationVideo(BaseModel):
    id: UUID
    owner_id: UUID
    visibility: Literal["public", "private"]
    processing_status: Literal["uploading", "processing", "ready", "failed"]
    moderation_status: Literal["visible", "hidden"]
    moderation_reason: str | None
    deleted: bool
    can_watch: bool
    title: str | None
    watch_url: str | None


class StaffReport(ReportReceipt):
    reporter_id: UUID
    decided_at: datetime | None
    decided_by: UUID | None
    decision_reason: str | None
    video: ModerationVideo


class VideoModerationResult(ModerationVideo):
    changed: bool


class ReportPage(BaseModel):
    items: list[StaffReport]
    next_cursor: str | None = None
