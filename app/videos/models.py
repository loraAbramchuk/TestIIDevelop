import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Video(Base):
    __tablename__ = "videos"
    __table_args__ = (
        CheckConstraint("visibility IN ('public', 'private')", name="valid_visibility"),
        CheckConstraint(
            "processing_status IN ('uploading', 'processing', 'ready', 'failed')",
            name="valid_processing_status",
        ),
        CheckConstraint(
            "moderation_status IN ('visible', 'hidden')", name="valid_moderation_status"
        ),
        CheckConstraint("char_length(btrim(title)) > 0", name="nonempty_title"),
        CheckConstraint("char_length(description) <= 5000", name="description_length"),
        CheckConstraint(
            "duration_seconds IS NULL OR (duration_seconds > 0 "
            "AND duration_seconds < 'Infinity'::double precision)",
            name="positive_finite_duration",
        ),
        CheckConstraint("width IS NULL OR width > 0", name="positive_width"),
        CheckConstraint("height IS NULL OR height > 0", name="positive_height"),
        CheckConstraint(
            "processing_status != 'ready' OR (duration_seconds IS NOT NULL "
            "AND width IS NOT NULL AND height IS NOT NULL)",
            name="ready_metadata",
        ),
        Index("ix_videos_owner_created", "owner_id", "created_at", "id"),
        Index(
            "ix_videos_public_created",
            "created_at",
            "id",
            postgresql_where=text(
                "visibility = 'public' AND processing_status = 'ready' "
                "AND moderation_status = 'visible' AND deleted_at IS NULL"
            ),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, server_default=text("''"))
    visibility: Mapped[str] = mapped_column(String(16), server_default=text("'private'"))
    processing_status: Mapped[str] = mapped_column(String(16), server_default=text("'uploading'"))
    moderation_status: Mapped[str] = mapped_column(String(16), server_default=text("'visible'"))
    duration_seconds: Mapped[float | None]
    width: Mapped[int | None]
    height: Mapped[int | None]
    error_code: Mapped[str | None] = mapped_column(String(64))
    moderation_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.clock_timestamp()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class VideoGrant(Base):
    __tablename__ = "video_grants"
    __table_args__ = (
        CheckConstraint("user_id != granted_by", name="not_self_grant"),
        Index("ix_video_grants_user_video", "user_id", "video_id"),
        Index("ix_video_grants_page", "video_id", "created_at", "user_id"),
    )

    video_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("videos.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    granted_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
