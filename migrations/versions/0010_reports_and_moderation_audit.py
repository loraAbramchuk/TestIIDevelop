"""reports and moderation audit

Revision ID: 0010
Revises: 0009
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: str | Sequence[str] | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "audit_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=False),
        sa.Column("object_type", sa.String(length=16), nullable=False),
        sa.Column("object_id", sa.Uuid(), nullable=False),
        sa.Column("action", sa.String(length=40), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "details",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "char_length(btrim(reason)) > 0 AND char_length(reason) <= 2000",
            name=op.f("ck_audit_events_reason_length"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_events")),
    )
    op.create_index(
        "ix_audit_events_object",
        "audit_events",
        ["object_type", "object_id", "created_at", "id"],
        unique=False,
    )
    op.create_index("ix_audit_events_page", "audit_events", ["created_at", "id"], unique=False)
    op.create_table(
        "reports",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("reporter_id", sa.Uuid(), nullable=False),
        sa.Column("video_id", sa.Uuid(), nullable=False),
        sa.Column("reason", sa.String(length=16), nullable=False),
        sa.Column("explanation", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("status", sa.String(length=16), server_default=sa.text("'open'"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by", sa.Uuid(), nullable=True),
        sa.Column("decision_reason", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "(status='open' AND decided_at IS NULL AND decided_by IS NULL "
            "AND decision_reason IS NULL) OR (status IN ('resolved','dismissed') AND "
            "decided_at IS NOT NULL AND decision_reason IS NOT NULL AND "
            "char_length(btrim(decision_reason)) > 0 AND char_length(decision_reason) <= 2000)",
            name=op.f("ck_reports_decision_state"),
        ),
        sa.CheckConstraint(
            "reason IN ('spam','prohibited','copyright','other')",
            name=op.f("ck_reports_valid_reason"),
        ),
        sa.CheckConstraint(
            "status IN ('open','resolved','dismissed')", name=op.f("ck_reports_valid_status")
        ),
        sa.CheckConstraint(
            "char_length(explanation) <= 2000", name=op.f("ck_reports_explanation_length")
        ),
        sa.ForeignKeyConstraint(
            ["decided_by"],
            ["users.id"],
            name=op.f("fk_reports_decided_by_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["reporter_id"],
            ["users.id"],
            name=op.f("fk_reports_reporter_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["video_id"],
            ["videos.id"],
            name=op.f("fk_reports_video_id_videos"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reports")),
    )
    op.create_index("ix_reports_page", "reports", ["created_at", "id"], unique=False)
    op.create_index(
        "ix_reports_status_page", "reports", ["status", "created_at", "id"], unique=False
    )
    op.create_index("ix_reports_video", "reports", ["video_id"], unique=False)
    op.create_index(
        "uq_reports_open_user_video",
        "reports",
        ["reporter_id", "video_id"],
        unique=True,
        postgresql_where=sa.text("status='open'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_reports_open_user_video",
        table_name="reports",
        postgresql_where=sa.text("status='open'"),
    )
    op.drop_index("ix_reports_video", table_name="reports")
    op.drop_index("ix_reports_status_page", table_name="reports")
    op.drop_index("ix_reports_page", table_name="reports")
    op.drop_table("reports")
    op.drop_index("ix_audit_events_page", table_name="audit_events")
    op.drop_index("ix_audit_events_object", table_name="audit_events")
    op.drop_table("audit_events")
