import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Channel(Base):
    __tablename__ = "channels"
    __table_args__ = (
        CheckConstraint("char_length(btrim(name)) > 0", name="nonempty_name"),
        CheckConstraint("char_length(description) <= 2000", name="description_length"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True
    )
    name: Mapped[str] = mapped_column(String(100))
    description: Mapped[str] = mapped_column(Text, server_default=text("''"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.clock_timestamp()
    )


class Subscription(Base):
    __tablename__ = "subscriptions"
    __table_args__ = (Index("ix_subscriptions_channel_id", "channel_id"),)

    subscriber_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    channel_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("channels.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
