"""Install the immutable Procrastinate 3.11.0 schema in its own namespace.

Revision ID: 0004
Revises: 0003
"""

import hashlib
from pathlib import Path

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = depends_on = None
SCHEMA_SHA256 = "d1f91ba563f92376c631a7d88cb54a5fdc849d5ff104fcc6d157a8a1227f779f"


def upgrade():
    sql = (Path(__file__).resolve().parent.parent / "sql/procrastinate-3.11.0.sql").read_bytes()
    if hashlib.sha256(sql).hexdigest() != SCHEMA_SHA256:
        raise RuntimeError("Procrastinate schema checksum mismatch")
    connection = op.get_bind().connection.driver_connection
    with connection.cursor() as cursor:
        cursor.execute("CREATE SCHEMA video_queue")
        cursor.execute("SET LOCAL search_path TO video_queue, public")
        cursor.execute(sql.decode())
        # Upstream functions use unqualified internal names. Pin their lookup path
        # so triggers also work when a qualified table is accessed from public.
        cursor.execute("""
            DO $migration$
            DECLARE routine record;
            BEGIN
                FOR routine IN
                    SELECT p.proname, pg_get_function_identity_arguments(p.oid) AS arguments
                    FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
                    WHERE n.nspname = 'video_queue' AND p.prokind = 'f'
                LOOP
                    EXECUTE format('ALTER FUNCTION video_queue.%I(%s) '
                        'SET search_path TO video_queue, public',
                        routine.proname, routine.arguments);
                END LOOP;
            END $migration$;
        """)
        cursor.execute("SET LOCAL search_path TO public")


def downgrade():
    op.execute("DROP SCHEMA video_queue CASCADE")
