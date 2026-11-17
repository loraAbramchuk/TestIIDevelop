import uuid
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class UploadAdmission(Base):
    __tablename__ = "upload_admission"
    __table_args__ = (CheckConstraint("id = 1", name="singleton"),)

    id: Mapped[int] = mapped_column(primary_key=True)


class UploadAttempt(Base):
    __tablename__ = "upload_attempts"
    __table_args__ = (
        CheckConstraint(
            "state IN ('receiving','processing','stored','cleanup','cleaned')", name="valid_state"
        ),
        CheckConstraint("reserved_bytes >= 0 AND actual_bytes >= 0", name="nonnegative_bytes"),
        CheckConstraint("expected_bytes IS NULL OR expected_bytes > 0", name="positive_expected"),
        CheckConstraint("state != 'receiving' OR lease_until IS NOT NULL", name="receiving_lease"),
        CheckConstraint(
            "state != 'processing' OR (job_id IS NOT NULL AND actual_bytes > 0)",
            name="processing_job",
        ),
        CheckConstraint("state != 'cleaned' OR reserved_bytes = 0", name="cleaned_reservation"),
        Index("ix_upload_attempts_owner_state", "owner_id", "state"),
        Index("ix_upload_attempts_state_lease", "state", "lease_until"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    video_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("videos.id", ondelete="RESTRICT"))
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    state: Mapped[str] = mapped_column(String(16), server_default=text("'receiving'"))
    reserved_bytes: Mapped[int] = mapped_column(BigInteger)
    actual_bytes: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    expected_bytes: Mapped[int | None] = mapped_column(BigInteger)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    job_id: Mapped[int | None] = mapped_column(BigInteger)
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
