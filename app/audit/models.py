import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Index, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        CheckConstraint(
            "char_length(btrim(reason)) > 0 AND char_length(reason) <= 2000", name="reason_length"
        ),
        Index("ix_audit_events_page", "created_at", "id"),
        Index("ix_audit_events_object", "object_type", "object_id", "created_at", "id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    # Deliberately not FKs: an actor/object deletion must not erase audit identity.
    actor_id: Mapped[uuid.UUID]
    object_type: Mapped[str] = mapped_column(String(16))
    object_id: Mapped[uuid.UUID]
    action: Mapped[str] = mapped_column(String(40))
    reason: Mapped[str] = mapped_column(Text)
    details: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
