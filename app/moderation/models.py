import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Report(Base):
    __tablename__ = "reports"
    __table_args__ = (
        CheckConstraint("status IN ('open','resolved','dismissed')", name="valid_status"),
        CheckConstraint("reason IN ('spam','prohibited','copyright','other')", name="valid_reason"),
        CheckConstraint("char_length(explanation) <= 2000", name="explanation_length"),
        CheckConstraint(
            "(status='open' AND decided_at IS NULL AND decided_by IS NULL "
            "AND decision_reason IS NULL) OR "
            "(status IN ('resolved','dismissed') AND decided_at IS NOT NULL AND "
            "decision_reason IS NOT NULL AND char_length(btrim(decision_reason)) > 0 AND "
            "char_length(decision_reason) <= 2000)",
            name="decision_state",
        ),
        Index(
            "uq_reports_open_user_video",
            "reporter_id",
            "video_id",
            unique=True,
            postgresql_where=text("status='open'"),
        ),
        Index("ix_reports_status_page", "status", "created_at", "id"),
        Index("ix_reports_page", "created_at", "id"),
        Index("ix_reports_video", "video_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    reporter_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    video_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("videos.id", ondelete="RESTRICT"))
    reason: Mapped[str] = mapped_column(String(16))
    explanation: Mapped[str] = mapped_column(Text, server_default=text("''"))
    status: Mapped[str] = mapped_column(String(16), server_default=text("'open'"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    decision_reason: Mapped[str | None] = mapped_column(Text)
