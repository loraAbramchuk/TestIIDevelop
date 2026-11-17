"""Managed UUID directories, exclusive file locks and atomic same-volume publication."""

import errno
import fcntl
import os
from pathlib import Path
from uuid import UUID


def attempt_key(identifier: UUID) -> str:
    return f"uploads/{identifier.hex}/source.mp4"


class UploadWriter:
    def __init__(self, root: Path, identifier: UUID):
        self.directory_fd = self.lock_fd = self.file_fd = None
        try:
            base = root / "uploads"
            base.mkdir(mode=0o700, exist_ok=True)
            root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(root_fd)
            finally:
                os.close(root_fd)
            base_fd = os.open(base, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                try:
                    os.mkdir(identifier.hex, mode=0o700, dir_fd=base_fd)
                except FileExistsError:
                    pass
                os.fsync(base_fd)
                self.directory_fd = os.open(
                    identifier.hex, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=base_fd
                )
            finally:
                os.close(base_fd)
            self.lock_fd = os.open(
                ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600, dir_fd=self.directory_fd
            )
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            self.close()
            raise

    def start(self):
        self.file_fd = os.open(
            "source.part",
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
            0o600,
            dir_fd=self.directory_fd,
        )

    def write(self, chunk):
        remaining = memoryview(chunk)
        while remaining:
            written = os.write(self.file_fd, remaining)
            if written == 0:
                raise OSError(errno.EIO, "No bytes written")
            remaining = remaining[written:]

    def finish(self):
        os.fsync(self.file_fd)
        os.close(self.file_fd)
        self.file_fd = None
        os.replace(
            "source.part", "source.mp4", src_dir_fd=self.directory_fd, dst_dir_fd=self.directory_fd
        )
        os.fsync(self.directory_fd)

    def close(self):
        for name in ("file_fd", "lock_fd", "directory_fd"):
            fd = getattr(self, name, None)
            if fd is not None:
                os.close(fd)
                setattr(self, name, None)

    def __del__(self):
        try:
            self.close()
        except OSError:
            pass


def remove_attempt(root: Path, identifier: UUID) -> bool:
    """Never release a reservation while a live writer holds the file lock."""
    base = root / "uploads"
    try:
        base_fd = os.open(base, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return True
    directory_fd = lock_fd = None
    try:
        try:
            directory_fd = os.open(
                identifier.hex, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=base_fd
            )
        except FileNotFoundError:
            return True
        lock_fd = os.open(
            ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600, dir_fd=directory_fd
        )
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        # Only our known names are removed, never arbitrary neighboring files or symlinks.
        for name in ("source.part", "source.mp4"):
            try:
                os.unlink(name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
        os.fsync(directory_fd)
        try:
            os.unlink(".lock", dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        os.rmdir(identifier.hex, dir_fd=base_fd)
        os.fsync(base_fd)
        return True
    finally:
        for fd in (lock_fd, directory_fd, base_fd):
            if fd is not None:
                os.close(fd)
