import signal

import pytest

from app.processing.errors import TransientProcessingError
from app.processing.media import tool_failure


@pytest.mark.parametrize(
    "diagnostic,code,retry",
    [
        (
            b"pthread_create() failed: Resource temporarily unavailable",
            "processing_resources_unavailable",
            True,
        ),
        (b"No space left on device", "processing_storage_full", True),
        (b"Read-only file system", "processing_storage_unavailable", True),
        (b"Cannot allocate memory", "processing_budget_exceeded", False),
        (b"Invalid NAL unit", "invalid_media", False),
    ],
)
def test_known_ffmpeg_failures_are_classified_and_redacted(diagnostic, code, retry):
    error = tool_failure(diagnostic + b" /private/customer/original.mp4", 1)
    assert error.code == code
    assert isinstance(error, TransientProcessingError) == retry
    assert "/private" not in str(error)


@pytest.mark.parametrize("status", [-signal.SIGXFSZ, -signal.SIGKILL, -signal.SIGXCPU])
def test_budget_signals_are_terminal_and_not_reported_as_bad_media(status):
    assert tool_failure(b"", status).code == "processing_budget_exceeded"
