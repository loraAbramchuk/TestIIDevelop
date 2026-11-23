import logging
from pathlib import Path
from tempfile import NamedTemporaryFile

logger = logging.getLogger(__name__)
MARKER = ".storage-readiness"
MARKER_CONTENT = b"video-platform-storage-v1\n"


def initialize_storage(root: Path) -> None:
    try:
        root.mkdir(parents=True, exist_ok=True)
        try:
            with (root / MARKER).open("xb") as file:
                file.write(MARKER_CONTENT)
        except FileExistsError:
            pass
    except OSError:
        # Existing read-only storage can still serve viewers; diagnostics report write failures.
        logger.warning("Storage initialization could not write readiness marker")


def check_storage_read(root: Path) -> None:
    if (root / MARKER).read_bytes() != MARKER_CONTENT:
        raise OSError("Invalid storage readiness marker")


def check_storage_write(root: Path) -> None:
    with NamedTemporaryFile(dir=root, prefix=".health-") as probe:
        probe.write(b"health")
        probe.flush()
