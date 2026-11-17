import argparse
import logging
import signal
import threading

from sqlalchemy.exc import SQLAlchemyError

from app.config import Settings
from app.db import make_engine, make_session_factory
from app.uploads.service import UploadService


def main():
    parser = argparse.ArgumentParser(
        description="Clean expired/failed upload attempts; no video decoding"
    )
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    settings = Settings()
    engine = make_engine(settings)
    service = UploadService(settings, make_session_factory(engine))
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    try:
        while not stop.is_set():
            try:
                count = service.sweep()
                if count:
                    logging.warning("Cleaned %s expired/failed upload attempts", count)
            except (SQLAlchemyError, OSError):
                logging.warning("Upload maintenance temporarily unavailable; will retry")
                if args.once:
                    raise
            if args.once:
                break
            stop.wait(settings.upload_cleanup_interval_seconds)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
