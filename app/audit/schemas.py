from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class AuditView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    actor_id: UUID
    object_type: str
    object_id: UUID
    action: str
    reason: str
    details: dict[str, str]
    created_at: datetime


class AuditPage(BaseModel):
    items: list[AuditView]
    next_cursor: str | None = None
