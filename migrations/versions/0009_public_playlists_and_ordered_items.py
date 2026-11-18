"""public playlists and ordered items

Revision ID: 0009
Revises: 0008
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | Sequence[str] | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "playlists",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("revision", sa.Uuid(), nullable=False),
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
        sa.CheckConstraint(
            "char_length(btrim(title)) > 0 AND char_length(title) <= 200",
            name=op.f("ck_playlists_title_length"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], name=op.f("fk_playlists_owner_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_playlists")),
    )
    op.create_index(
        "ix_playlists_owner_page", "playlists", ["owner_id", "created_at", "id"], unique=False
    )
    op.create_table(
        "playlist_items",
        sa.Column("playlist_id", sa.Uuid(), nullable=False),
        sa.Column("video_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.CheckConstraint("position >= 0", name=op.f("ck_playlist_items_nonnegative_position")),
        sa.ForeignKeyConstraint(
            ["playlist_id"],
            ["playlists.id"],
            name=op.f("fk_playlist_items_playlist_id_playlists"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["video_id"],
            ["videos.id"],
            name=op.f("fk_playlist_items_video_id_videos"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("playlist_id", "video_id", name=op.f("pk_playlist_items")),
        sa.UniqueConstraint(
            "playlist_id",
            "position",
            deferrable=True,
            initially="IMMEDIATE",
            name="uq_playlist_items_position",
        ),
    )
    op.create_index("ix_playlist_items_video", "playlist_items", ["video_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_playlist_items_video", table_name="playlist_items")
    op.drop_table("playlist_items")
    op.drop_index("ix_playlists_owner_page", table_name="playlists")
    op.drop_table("playlists")
