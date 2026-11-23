import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Playlist(Base):
    __tablename__ = "playlists"
    __table_args__ = (
        CheckConstraint(
            "char_length(btrim(title)) > 0 AND char_length(title) <= 200", name="title_length"
        ),
        Index("ix_playlists_owner_page", "owner_id", "created_at", "id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(Text)
    revision: Mapped[uuid.UUID] = mapped_column(default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.clock_timestamp()
    )


class PlaylistItem(Base):
    __tablename__ = "playlist_items"
    __table_args__ = (
        UniqueConstraint(
            "playlist_id",
            "position",
            name="uq_playlist_items_position",
            deferrable=True,
            initially="IMMEDIATE",
        ),
        CheckConstraint("position >= 0", name="nonnegative_position"),
        Index("ix_playlist_items_video", "video_id"),
    )

    playlist_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("playlists.id", ondelete="CASCADE"), primary_key=True
    )
    video_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("videos.id", ondelete="CASCADE"), primary_key=True
    )
    position: Mapped[int]
