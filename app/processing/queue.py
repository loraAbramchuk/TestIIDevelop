from datetime import UTC, datetime

from procrastinate.manager import JobManager
from procrastinate.retry import BaseRetryStrategy, RetryDecision

from app.processing.errors import ProcessingBusy, TransientProcessingError


class ProcessingRetry(BaseRetryStrategy):
    def __init__(self, settings):
        self.settings = settings

    def get_retry_decision(self, *, exception, job):
        # Waiting for a physical slot did not execute media work and consumes no attempt.
        if isinstance(exception, ProcessingBusy) or (
            isinstance(exception, TransientProcessingError)
            and job.attempts < self.settings.processing_max_attempts
        ):
            return RetryDecision(retry_in={"seconds": self.settings.processing_retry_seconds})
        return None


class FencedJobManager(JobManager):
    """Keep late acknowledgements from an old worker off a recovered library job."""

    async def finish_job(self, job, status, delete_job):
        await self.connector.execute_query_async(
            query="""
            WITH owned AS MATERIALIZED (
                SELECT id FROM procrastinate_jobs
                WHERE id=%(id)s AND worker_id=%(worker)s AND attempts=%(attempt)s
                  AND status='doing' FOR UPDATE
            ) SELECT procrastinate_finish_job_v1(
                id, %(status)s::procrastinate_job_status, %(delete)s) FROM owned
        """,
            id=job.id,
            worker=job.worker_id,
            attempt=job.attempts,
            status=status.value,
            delete=delete_job,
        )

    async def retry_job(self, job, retry_at=None, priority=None, queue=None, lock=None):
        await self.connector.execute_query_async(
            query="""
            WITH owned AS MATERIALIZED (
                SELECT id FROM procrastinate_jobs
                WHERE id=%(id)s AND worker_id=%(worker)s AND attempts=%(attempt)s
                  AND status='doing' FOR UPDATE
            ) SELECT procrastinate_retry_job_v2(id, %(at)s, %(priority)s, %(queue)s, %(lock)s)
              FROM owned
        """,
            id=job.id,
            worker=job.worker_id,
            attempt=job.attempts,
            at=retry_at or datetime.now(UTC),
            priority=priority,
            queue=queue,
            lock=lock,
        )
