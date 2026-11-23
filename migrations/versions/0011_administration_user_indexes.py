"""Indexes for administrator user lists.

Revision ID: 0011
Revises: 0010
"""

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index("ix_users_page", "users", ["created_at", "id"])
    op.create_index("ix_users_role_page", "users", ["role", "created_at", "id"])


def downgrade():
    op.drop_index("ix_users_role_page", table_name="users")
    op.drop_index("ix_users_page", table_name="users")
