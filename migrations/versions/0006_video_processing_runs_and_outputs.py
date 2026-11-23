"""video_processing_runs_and_outputs

Revision ID: 0006
Revises: 0005
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "processing_workers",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("capacity", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_processing_workers")),
    )
    op.create_table(
        "processing_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("upload_id", sa.Uuid(), nullable=False),
        sa.Column("video_id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "state != 'running' OR lease_until IS NOT NULL",
            name=op.f("ck_processing_runs_running_lease"),
        ),
        sa.CheckConstraint(
            "state IN ('running','complete','cleanup','cleaned')",
            name=op.f("ck_processing_runs_valid_state"),
        ),
        sa.ForeignKeyConstraint(
            ["upload_id"],
            ["upload_attempts.id"],
            name=op.f("fk_processing_runs_upload_id_upload_attempts"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["video_id"],
            ["videos.id"],
            name=op.f("fk_processing_runs_video_id_videos"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_processing_runs")),
    )
    op.create_index(
        "ix_processing_runs_state_lease", "processing_runs", ["state", "lease_until"], unique=False
    )
    op.create_index("ix_processing_runs_upload", "processing_runs", ["upload_id"], unique=False)
    op.add_column(
        "upload_attempts",
        sa.Column("recovery_attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column("upload_attempts", sa.Column("current_run_id", sa.Uuid(), nullable=True))
    op.add_column(
        "upload_attempts",
        sa.Column("processing_attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column("videos", sa.Column("playback_key", sa.String(length=160), nullable=True))
    op.add_column("videos", sa.Column("thumbnail_key", sa.String(length=160), nullable=True))
    op.add_column("videos", sa.Column("playback_bytes", sa.BigInteger(), nullable=True))
    op.add_column("videos", sa.Column("thumbnail_bytes", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column("videos", "thumbnail_bytes")
    op.drop_column("videos", "playback_bytes")
    op.drop_column("videos", "thumbnail_key")
    op.drop_column("videos", "playback_key")
    op.drop_column("upload_attempts", "processing_attempts")
    op.drop_column("upload_attempts", "current_run_id")
    op.drop_column("upload_attempts", "recovery_attempts")
    op.drop_index("ix_processing_runs_upload", table_name="processing_runs")
    op.drop_index("ix_processing_runs_state_lease", table_name="processing_runs")
    op.drop_table("processing_runs")
    op.drop_table("processing_workers")
