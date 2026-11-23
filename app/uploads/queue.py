import procrastinate
from sqlalchemy import text


def queue_app(settings):
    return procrastinate.App(
        connector=procrastinate.PsycopgConnector(
            kwargs={
                "host": settings.db_host,
                "port": settings.db_port,
                "dbname": settings.postgres_db,
                "user": settings.postgres_user,
                "password": settings.postgres_password.get_secret_value(),
                "options": (
                    "-csearch_path=video_queue,public -cstatement_timeout=5000 -clock_timeout=3000"
                ),
                "connect_timeout": 3,
            },
            min_size=1,
            max_size=3,
        )
    )


def enqueue_processing(db, settings, video_id, attempt_id) -> int:
    # SET LOCAL is reset by commit/rollback and never leaks into a pooled connection.
    db.execute(text("SET LOCAL search_path TO video_queue, public"))
    raw = db.connection().connection.driver_connection
    return (
        queue_app(settings)
        .configure_task(
            "videos.process",
            connection=raw,
            queue="video-processing",
        )
        .defer(video_id=str(video_id), attempt_id=str(attempt_id))
    )


def cancel_processing(db, settings, job_ids):
    if not job_ids:
        return
    db.execute(text("SET LOCAL search_path TO video_queue, public"))
    raw = db.connection().connection.driver_connection
    manager = queue_app(settings).job_manager
    for job_id in sorted(set(job_ids)):
        manager.cancel_job_by_id(job_id=job_id, abort=True, connection=raw)
