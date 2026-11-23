import fcntl
import os
from pathlib import Path
from uuid import UUID

from app.processing.errors import ProcessingBusy

FILES = ("playback.part", "thumbnail.part", "playback.mp4", "thumbnail.jpg")


def keys(identifier: UUID) -> tuple[str, str]:
    return (
        f"processing/{identifier.hex}/playback.mp4",
        f"processing/{identifier.hex}/thumbnail.jpg",
    )


def directory(root: Path) -> int:
    root.mkdir(mode=0o700, exist_ok=True)
    parent = os.open(root.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(parent)
    finally:
        os.close(parent)
    return os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)


class Slot:
    def __init__(self, root: Path, capacity: int):
        self.fd = None
        base = directory(root / "processing-slots")
        try:
            for index in range(capacity):
                fd = os.open(
                    f"{index}.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600, dir_fd=base
                )
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    os.close(fd)
                else:
                    self.fd = fd
                    break
            if self.fd is None:
                raise ProcessingBusy("processing_slots_full")
        finally:
            os.close(base)

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


class RunDirectory:
    def __init__(self, root: Path, identifier: UUID):
        self.fd = self.lock_fd = None
        self.identifier = identifier
        base = directory(root / "processing")
        try:
            os.mkdir(identifier.hex, mode=0o700, dir_fd=base)
            os.fsync(base)
            self.fd = os.open(
                identifier.hex, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=base
            )
            self.lock_fd = os.open(
                ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600, dir_fd=self.fd
            )
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            self.close()
            raise
        finally:
            os.close(base)

    def path(self, name: str) -> str:
        assert name in FILES
        return f"/proc/self/fd/{self.fd}/{name}"

    def size(self, name: str) -> int:
        return os.stat(name, dir_fd=self.fd, follow_symlinks=False).st_size

    def publish(self):
        for old, new in (("playback.part", "playback.mp4"), ("thumbnail.part", "thumbnail.jpg")):
            fd = os.open(old, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=self.fd)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(old, new, src_dir_fd=self.fd, dst_dir_fd=self.fd)
        os.fsync(self.fd)

    def close(self):
        for name in ("lock_fd", "fd"):
            fd = getattr(self, name)
            if fd is not None:
                os.close(fd)
                setattr(self, name, None)


def remove_run(root: Path, identifier: UUID) -> bool:
    try:
        base = os.open(root / "processing", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return True
    fd = lock_fd = None
    try:
        try:
            fd = os.open(identifier.hex, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=base)
        except FileNotFoundError:
            return True
        lock_fd = os.open(".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        for name in FILES:
            try:
                os.unlink(name, dir_fd=fd)
            except FileNotFoundError:
                pass
        os.fsync(fd)
        os.unlink(".lock", dir_fd=fd)
        os.rmdir(identifier.hex, dir_fd=base)
        os.fsync(base)
        return True
    finally:
        for descriptor in (lock_fd, fd, base):
            if descriptor is not None:
                os.close(descriptor)
