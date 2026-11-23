"""upload_attempts_and_admission

Revision ID: 0005
Revises: 0004
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "upload_admission",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.CheckConstraint("id = 1", name=op.f("ck_upload_admission_singleton")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_upload_admission")),
    )
    op.execute("INSERT INTO upload_admission (id) VALUES (1)")
    op.create_table(
        "upload_attempts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("video_id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column(
            "state", sa.String(length=16), server_default=sa.text("'receiving'"), nullable=False
        ),
        sa.Column("reserved_bytes", sa.BigInteger(), nullable=False),
        sa.Column("actual_bytes", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("expected_bytes", sa.BigInteger(), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("job_id", sa.BigInteger(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "state != 'cleaned' OR reserved_bytes = 0",
            name=op.f("ck_upload_attempts_cleaned_reservation"),
        ),
        sa.CheckConstraint(
            "state != 'processing' OR (job_id IS NOT NULL AND actual_bytes > 0)",
            name=op.f("ck_upload_attempts_processing_job"),
        ),
        sa.CheckConstraint(
            "state != 'receiving' OR lease_until IS NOT NULL",
            name=op.f("ck_upload_attempts_receiving_lease"),
        ),
        sa.CheckConstraint(
            "state IN ('receiving','processing','stored','cleanup','cleaned')",
            name=op.f("ck_upload_attempts_valid_state"),
        ),
        sa.CheckConstraint(
            "expected_bytes IS NULL OR expected_bytes > 0",
            name=op.f("ck_upload_attempts_positive_expected"),
        ),
        sa.CheckConstraint(
            "reserved_bytes >= 0 AND actual_bytes >= 0",
            name=op.f("ck_upload_attempts_nonnegative_bytes"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["users.id"],
            name=op.f("fk_upload_attempts_owner_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["video_id"],
            ["videos.id"],
            name=op.f("fk_upload_attempts_video_id_videos"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_upload_attempts")),
    )
    op.create_index(
        "ix_upload_attempts_owner_state", "upload_attempts", ["owner_id", "state"], unique=False
    )
    op.create_index(
        "ix_upload_attempts_state_lease", "upload_attempts", ["state", "lease_until"], unique=False
    )
    op.add_column("videos", sa.Column("current_upload_id", sa.Uuid(), nullable=True))
    op.add_column("videos", sa.Column("source_key", sa.String(length=160), nullable=True))
    op.add_column("videos", sa.Column("source_bytes", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        "fk_videos_current_upload_id_upload_attempts",
        "videos",
        "upload_attempts",
        ["current_upload_id"],
        ["id"],
        ondelete="SET NULL",
        use_alter=True,
    )


def downgrade() -> None:
    op.drop_constraint("fk_videos_current_upload_id_upload_attempts", "videos", type_="foreignkey")
    op.drop_column("videos", "source_bytes")
    op.drop_column("videos", "source_key")
    op.drop_column("videos", "current_upload_id")
    op.drop_index("ix_upload_attempts_state_lease", table_name="upload_attempts")
    op.drop_index("ix_upload_attempts_owner_state", table_name="upload_attempts")
    op.drop_table("upload_attempts")
    op.drop_table("upload_admission")
