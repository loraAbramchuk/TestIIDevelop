from datetime import timedelta
from pathlib import Path
from uuid import UUID

from sqlalchemy import func, select

from app.config import Settings
from app.db import make_engine, make_session_factory
from app.models import ProcessingWorker


def live_workers(db, settings, identifier=None):
    query = (
        select(func.count())
        .select_from(ProcessingWorker)
        .where(
            ProcessingWorker.heartbeat_at
            > func.clock_timestamp() - timedelta(seconds=settings.processing_lease_seconds)
        )
    )
    if identifier is not None:
        query = query.where(ProcessingWorker.id == identifier)
    return db.scalar(query)


def main():
    settings = Settings()
    engine = make_engine(settings, query_timeout=True)
    try:
        identifier = UUID(Path("/tmp/processing-worker-id").read_text().strip())
        with make_session_factory(engine)() as db:
            if not live_workers(db, settings, identifier):
                raise SystemExit(1)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
