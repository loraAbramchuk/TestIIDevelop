from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class ProcessingRun(Base):
    __tablename__ = "processing_runs"
    __table_args__ = (
        CheckConstraint("state IN ('running','complete','cleanup','cleaned')", name="valid_state"),
        CheckConstraint("state != 'running' OR lease_until IS NOT NULL", name="running_lease"),
        Index("ix_processing_runs_upload", "upload_id"),
        Index("ix_processing_runs_state_lease", "state", "lease_until"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    upload_id: Mapped[UUID] = mapped_column(ForeignKey("upload_attempts.id", ondelete="RESTRICT"))
    video_id: Mapped[UUID] = mapped_column(ForeignKey("videos.id", ondelete="RESTRICT"))
    job_id: Mapped[int] = mapped_column(BigInteger)
    state: Mapped[str] = mapped_column(String(16), default="running")
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ProcessingWorker(Base):
    __tablename__ = "processing_workers"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    capacity: Mapped[int]
