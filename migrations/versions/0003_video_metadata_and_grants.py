"""Метаданные видео и частные разрешения.

Revision ID: 0003
Revises: 0002
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "videos",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column(
            "visibility", sa.String(length=16), server_default=sa.text("'private'"), nullable=False
        ),
        sa.Column(
            "processing_status",
            sa.String(length=16),
            server_default=sa.text("'uploading'"),
            nullable=False,
        ),
        sa.Column(
            "moderation_status",
            sa.String(length=16),
            server_default=sa.text("'visible'"),
            nullable=False,
        ),
        sa.Column("duration_seconds", sa.Double(), nullable=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("moderation_reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "duration_seconds IS NULL OR (duration_seconds > 0 "
            "AND duration_seconds < 'Infinity'::double precision)",
            name=op.f("ck_videos_positive_finite_duration"),
        ),
        sa.CheckConstraint(
            "moderation_status IN ('visible', 'hidden')",
            name=op.f("ck_videos_valid_moderation_status"),
        ),
        sa.CheckConstraint(
            "processing_status != 'ready' OR (duration_seconds IS NOT NULL "
            "AND width IS NOT NULL AND height IS NOT NULL)",
            name=op.f("ck_videos_ready_metadata"),
        ),
        sa.CheckConstraint(
            "processing_status IN ('uploading', 'processing', 'ready', 'failed')",
            name=op.f("ck_videos_valid_processing_status"),
        ),
        sa.CheckConstraint(
            "visibility IN ('public', 'private')", name=op.f("ck_videos_valid_visibility")
        ),
        sa.CheckConstraint("char_length(btrim(title)) > 0", name=op.f("ck_videos_nonempty_title")),
        sa.CheckConstraint(
            "char_length(description) <= 5000", name=op.f("ck_videos_description_length")
        ),
        sa.CheckConstraint("height IS NULL OR height > 0", name=op.f("ck_videos_positive_height")),
        sa.CheckConstraint("width IS NULL OR width > 0", name=op.f("ck_videos_positive_width")),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], name=op.f("fk_videos_owner_id_users"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_videos")),
    )
    op.create_index(
        "ix_videos_owner_created", "videos", ["owner_id", "created_at", "id"], unique=False
    )
    op.create_index(
        "ix_videos_public_created",
        "videos",
        ["created_at", "id"],
        unique=False,
        postgresql_where=sa.text(
            "visibility = 'public' AND processing_status = 'ready' "
            "AND moderation_status = 'visible' AND deleted_at IS NULL"
        ),
    )
    op.create_table(
        "video_grants",
        sa.Column("video_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("granted_by", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("user_id != granted_by", name=op.f("ck_video_grants_not_self_grant")),
        sa.ForeignKeyConstraint(
            ["granted_by"],
            ["users.id"],
            name=op.f("fk_video_grants_granted_by_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_video_grants_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["video_id"],
            ["videos.id"],
            name=op.f("fk_video_grants_video_id_videos"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("video_id", "user_id", name=op.f("pk_video_grants")),
    )
    op.create_index(
        "ix_video_grants_page", "video_grants", ["video_id", "created_at", "user_id"], unique=False
    )
    op.create_index(
        "ix_video_grants_user_video", "video_grants", ["user_id", "video_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_video_grants_user_video", table_name="video_grants")
    op.drop_index("ix_video_grants_page", table_name="video_grants")
    op.drop_table("video_grants")
    op.drop_index(
        "ix_videos_public_created",
        table_name="videos",
        postgresql_where=sa.text(
            "visibility = 'public' AND processing_status = 'ready' "
            "AND moderation_status = 'visible' AND deleted_at IS NULL"
        ),
    )
    op.drop_index("ix_videos_owner_created", table_name="videos")
    op.drop_table("videos")
