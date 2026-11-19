"""Email confirmation and one-use password recovery links."""

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade():
    # Existing addresses are not silently declared verified.
    op.add_column(
        "users", sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_table(
        "auth_mail_tokens",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("purpose", sa.String(20), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True)),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "purpose IN ('verify_email','reset_password')", name="mail_token_purpose"
        ),
    )
    op.create_index("ix_auth_mail_tokens_expires_at", "auth_mail_tokens", ["expires_at"])
    op.create_index("ix_auth_mail_tokens_user_purpose", "auth_mail_tokens", ["user_id", "purpose"])


def downgrade():
    op.drop_table("auth_mail_tokens")
    op.drop_column("users", "email_verified_at")
