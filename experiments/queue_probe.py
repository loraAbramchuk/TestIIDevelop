"""Destructive setup is allowed only in a fresh, disposable database ending in _probe.

Run: POSTGRES_DB=stage1_queue_probe uv run --group experiments python experiments/queue_probe.py
This experiment never installs a production video worker or production queue schema.
"""

import argparse
import asyncio
import json
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import procrastinate  # noqa: E402
from sqlalchemy import Column, Integer, MetaData, String, Table, insert, inspect, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.config import Settings  # noqa: E402
from app.db import make_engine  # noqa: E402

metadata = MetaData()
videos = Table(
    "stage1_probe_videos",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("state", String(20), nullable=False),
    Column("executions", Integer, nullable=False, default=0),
)


def make_queue(settings):
    return procrastinate.App(
        connector=procrastinate.PsycopgConnector(
            kwargs={
                "host": settings.db_host,
                "port": settings.db_port,
                "dbname": settings.postgres_db,
                "user": settings.postgres_user,
                "password": settings.postgres_password.get_secret_value(),
                "connect_timeout": 3,
            },
            min_size=1,
            max_size=3,
        )
    )


def wait_for(predicate, seconds=15):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("Queue experiment timed out")


def run_worker(settings):
    queue = make_queue(settings)
    engine = make_engine(settings)

    @queue.task(name="stage1.process", queue="stage1")
    async def process(video_id: str, crash_once: bool = False):
        with engine.begin() as connection:
            count = connection.execute(
                videos.update()
                .where(videos.c.id == video_id)
                .values(executions=videos.c.executions + 1)
                .returning(videos.c.executions)
            ).scalar_one()
        if crash_once and count == 1:
            await asyncio.sleep(3600)  # The parent kills this exact process with SIGKILL.
        with engine.begin() as connection:
            connection.execute(videos.update().where(videos.c.id == video_id).values(state="done"))

    try:
        queue.run_worker(
            queues=["stage1"],
            concurrency=1,
            wait=False,
            fetch_job_polling_interval=0.1,
            listen_notify=False,
            update_heartbeat_interval=0.2,
            stalled_worker_timeout=1,
        )
    finally:
        engine.dispose()


def run_probe(settings):
    engine = make_engine(settings)
    queue = make_queue(settings)
    if inspect(engine).get_table_names():
        raise SystemExit("Refusing schema setup: create a fresh empty disposable *_probe database")
    queue.open()
    queue.schema_manager.apply_schema()
    metadata.create_all(engine)
    children = []
    log = tempfile.TemporaryFile()

    def scalar(sql, **params):
        with engine.connect() as connection:
            return connection.execute(text(sql), params).scalar_one()

    def enqueue(session, video_id, crash_once=False):
        raw = session.connection().connection.driver_connection
        return queue.configure_task("stage1.process", connection=raw, queue="stage1").defer(
            video_id=video_id,
            crash_once=crash_once,
        )

    def start_worker():
        child = subprocess.Popen([sys.executable, __file__, "--worker"], stdout=log, stderr=log)
        children.append(child)
        return child

    try:
        rolled_back = str(uuid.uuid4())
        with Session(engine) as session:
            session.execute(insert(videos).values(id=rolled_back, state="pending"))
            rolled_job = enqueue(session, rolled_back)
            assert (
                scalar("SELECT count(*) FROM procrastinate_jobs WHERE id=:id", id=rolled_job) == 0
            )
            session.rollback()
        assert scalar("SELECT count(*) FROM stage1_probe_videos WHERE id=:id", id=rolled_back) == 0
        assert scalar("SELECT count(*) FROM procrastinate_jobs WHERE id=:id", id=rolled_job) == 0

        normal = str(uuid.uuid4())
        with Session(engine) as session:
            session.execute(insert(videos).values(id=normal, state="pending"))
            normal_job = enqueue(session, normal)
            assert (
                scalar("SELECT count(*) FROM procrastinate_jobs WHERE id=:id", id=normal_job) == 0
            )
            session.commit()
        assert scalar("SELECT count(*) FROM procrastinate_jobs WHERE id=:id", id=normal_job) == 1
        worker = start_worker()
        worker.wait(timeout=15)
        assert worker.returncode == 0
        assert (
            scalar("SELECT status::text FROM procrastinate_jobs WHERE id=:id", id=normal_job)
            == "succeeded"
        )
        assert scalar("SELECT state FROM stage1_probe_videos WHERE id=:id", id=normal) == "done"

        interrupted = str(uuid.uuid4())
        with Session(engine) as session:
            session.execute(insert(videos).values(id=interrupted, state="pending"))
            interrupted_job = enqueue(session, interrupted, crash_once=True)
            session.commit()
        worker = start_worker()
        wait_for(
            lambda: (
                scalar("SELECT executions FROM stage1_probe_videos WHERE id=:id", id=interrupted)
                == 1
            )
        )
        worker.kill()
        worker.wait(timeout=5)
        assert worker.returncode < 0
        assert (
            scalar("SELECT status::text FROM procrastinate_jobs WHERE id=:id", id=interrupted_job)
            == "doing"
        )

        async def recover():
            async with queue.open_async():
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    stalled = await queue.job_manager.get_stalled_jobs(
                        queue="stage1",
                        seconds_since_heartbeat=1,
                    )
                    if any(job.id == interrupted_job for job in stalled):
                        await queue.job_manager.retry_job_by_id_async(
                            interrupted_job, retry_at=datetime.now(UTC)
                        )
                        return
                    await asyncio.sleep(0.1)
                raise AssertionError("Killed worker was not detected through heartbeat")

        asyncio.run(recover())
        worker = start_worker()
        worker.wait(timeout=15)
        assert worker.returncode == 0
        assert (
            scalar("SELECT status::text FROM procrastinate_jobs WHERE id=:id", id=interrupted_job)
            == "succeeded"
        )
        assert (
            scalar("SELECT executions FROM stage1_probe_videos WHERE id=:id", id=interrupted) == 2
        )
        assert (
            scalar("SELECT state FROM stage1_probe_videos WHERE id=:id", id=interrupted) == "done"
        )
        print(
            json.dumps(
                {
                    "procrastinate": procrastinate.__version__,
                    "atomic_commit": "passed",
                    "atomic_rollback": "passed",
                    "invisible_before_commit": "passed",
                    "worker_execution": "passed",
                    "sigkill_detection": "passed",
                    "explicit_stalled_recovery": "passed",
                    "reexecution_count": 2,
                    "limitations": [
                        "Recovery requires a periodic sweep, not just worker restart",
                        "Domain fencing and idempotent file publication belong to stage 4",
                    ],
                },
                indent=2,
            )
        )
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
        log.close()
        queue.close()
        engine.dispose()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", action="store_true")
    args = parser.parse_args()
    settings = Settings()
    if not settings.postgres_db.endswith("_probe"):
        parser.exit(1, "POSTGRES_DB must name a disposable database ending in _probe\n")
    if args.worker:
        run_worker(settings)
    else:
        run_probe(settings)


if __name__ == "__main__":
    main()
