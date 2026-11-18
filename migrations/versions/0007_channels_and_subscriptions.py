"""Channel profiles, existing-account backfill, unique subscriptions.

Revision ID: 0007
Revises: 0006
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade():
    op.create_table(
        "channels",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("description", sa.Text(), server_default=sa.text("''"), nullable=False),
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
        sa.CheckConstraint("char_length(btrim(name)) > 0", name=op.f("ck_channels_nonempty_name")),
        sa.CheckConstraint(
            "char_length(description) <= 2000", name=op.f("ck_channels_description_length")
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], ondelete="CASCADE", name=op.f("fk_channels_owner_id_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_channels")),
        sa.UniqueConstraint("owner_id", name=op.f("uq_channels_owner_id")),
    )
    op.execute("""
        INSERT INTO channels(id, owner_id, name)
        SELECT gen_random_uuid(), id,
               coalesce(nullif(btrim(display_name), ''), 'Канал автора') FROM users
    """)
    op.create_table(
        "subscriptions",
        sa.Column("subscriber_id", sa.Uuid(), nullable=False),
        sa.Column("channel_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["subscriber_id"],
            ["users.id"],
            ondelete="CASCADE",
            name=op.f("fk_subscriptions_subscriber_id_users"),
        ),
        sa.ForeignKeyConstraint(
            ["channel_id"],
            ["channels.id"],
            ondelete="CASCADE",
            name=op.f("fk_subscriptions_channel_id_channels"),
        ),
        sa.PrimaryKeyConstraint("subscriber_id", "channel_id", name=op.f("pk_subscriptions")),
    )
    op.create_index("ix_subscriptions_channel_id", "subscriptions", ["channel_id"])


def downgrade():
    op.drop_table("subscriptions")
    op.drop_table("channels")
