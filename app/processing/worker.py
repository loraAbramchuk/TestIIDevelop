import asyncio
import logging
import shutil
import threading
from pathlib import Path
from uuid import uuid4

from app.config import Settings
from app.db import make_engine, make_session_factory
from app.processing.queue import FencedJobManager, ProcessingRetry
from app.processing.runner import process_video
from app.processing.service import ProcessingService
from app.storage_health import initialize_storage
from app.uploads.queue import queue_app


def build_worker(settings, sessions):
    queue = queue_app(settings)
    queue.job_manager = FencedJobManager(queue.connector)

    @queue.task(
        name="videos.process",
        queue="video-processing",
        pass_context=True,
        retry=ProcessingRetry(settings),
    )
    async def process(context, video_id, attempt_id):
        cancel = threading.Event()
        task = asyncio.create_task(
            asyncio.to_thread(
                process_video,
                settings,
                sessions,
                context.job.id,
                video_id,
                attempt_id,
                lambda: cancel.is_set() or context.should_abort(),
            )
        )
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancel.set()
            # Stop FFmpeg and wait for all writes before the library acknowledges cancellation.
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if task.done() and not task.cancelled():
                task.exception()
            raise

    return queue


async def run(settings):
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            raise RuntimeError(f"Required media tool is unavailable: {tool}")
    initialize_storage(settings.media_root)
    engine = make_engine(settings, query_timeout=True)
    sessions = make_session_factory(engine)
    service = ProcessingService(settings, sessions)
    identifier = uuid4()
    queue = build_worker(settings, sessions)

    async def heartbeat():
        while True:
            pending = asyncio.create_task(asyncio.to_thread(service.heartbeat, identifier))
            try:
                await asyncio.shield(pending)
            except asyncio.CancelledError:
                await pending
                raise
            await asyncio.sleep(settings.processing_heartbeat_seconds)

    try:
        async with queue.open_async():
            await asyncio.to_thread(service.heartbeat, identifier)
            Path("/tmp/processing-worker-id").write_text(str(identifier))
            monitor = asyncio.create_task(heartbeat())
            work = asyncio.create_task(
                queue.run_worker_async(
                    queues=["video-processing"],
                    concurrency=settings.processing_concurrency,
                    listen_notify=False,
                    fetch_job_polling_interval=1,
                    update_heartbeat_interval=settings.processing_heartbeat_seconds,
                    stalled_worker_timeout=settings.processing_lease_seconds,
                    shutdown_graceful_timeout=10,
                )
            )
            try:
                done, _ = await asyncio.wait({work, monitor}, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            finally:
                monitor.cancel()
                if not work.done():
                    work.cancel()
                await asyncio.gather(work, monitor, return_exceptions=True)
    finally:
        # Remove our heartbeat on a normal exit; SIGKILL is detected by TTL.
        try:
            from app.models import ProcessingWorker

            with sessions() as db:
                db.execute(
                    ProcessingWorker.__table__.delete().where(ProcessingWorker.id == identifier)
                )
                db.commit()
        finally:
            engine.dispose()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(run(Settings()))


if __name__ == "__main__":
    main()
