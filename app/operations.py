"""Aggregate operator diagnostics, available only through the local CLI."""

import json
from shutil import disk_usage

from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError

from app.config import Settings
from app.db import make_engine, make_session_factory
from app.models import UploadAttempt
from app.processing.health import live_workers


def snapshot(settings):
    result = {}
    try:
        disk = disk_usage(settings.media_root)
        result["disk"] = {
            "total": disk.total,
            "used": disk.used,
            "free": disk.free,
            "reserve": settings.media_min_free_bytes,
        }
    except OSError:
        result["disk"] = {"status": "unavailable"}
    engine = make_engine(settings, query_timeout=True)
    try:
        with make_session_factory(engine)() as db:
            result["queue"] = dict(
                db.execute(
                    text(
                        "SELECT status::text, count(*) FROM video_queue.procrastinate_jobs "
                        "GROUP BY status"
                    )
                ).all()
            )
            result["uploads"] = dict(
                db.execute(
                    select(UploadAttempt.state, func.count()).group_by(UploadAttempt.state)
                ).all()
            )
            result["live_workers"] = live_workers(db, settings)
    except SQLAlchemyError:
        result["database"] = "unavailable"
    finally:
        engine.dispose()
    return result


if __name__ == "__main__":
    print(json.dumps(snapshot(Settings())))
