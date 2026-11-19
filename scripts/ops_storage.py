"""Space accounting and bounded subprocess output for operator snapshots."""

import os
import selectors
import shutil
import signal
import subprocess
import time
from pathlib import Path

DEFAULT_RESERVE = 2 * 1024**3
COPY_CHUNK = 1024**2
ARCHIVE_OVERHEAD = 64 * 1024**2


class OpsError(Exception):
    pass


class SpaceBudget:
    def __init__(self, path, reserve=DEFAULT_RESERVE):
        self.path = Path(path)
        self.reserve = int(reserve)
        if self.reserve < 0:
            raise OpsError("Disk reserve must not be negative")

    def require(self, size=0):
        if size < 0:
            raise OpsError("Invalid snapshot size")
        free = shutil.disk_usage(self.path).free
        if free < size + self.reserve:
            raise OpsError(
                f"Insufficient snapshot space: need {size} bytes plus "
                f"{self.reserve} reserve; available {free}"
            )


class GuardedWriter:
    def __init__(self, file, budget):
        self.file = file
        self.budget = budget

    def write(self, data):
        # Check every bounded chunk, including after another process consumes space.
        # Flush so the next disk_usage includes buffered writes from this process.
        total = 0
        for offset in range(0, len(data), COPY_CHUNK):
            chunk = data[offset : offset + COPY_CHUNK]
            self.budget.require(len(chunk) + 8192)
            total += self.file.write(chunk)
            self.file.flush()
        return total

    def flush(self):
        self.file.flush()


class GuardedReader:
    def __init__(self, file, check):
        self.file = file
        self.check_callback = check
        self.next_check = 0

    def check(self):
        now = time.monotonic()
        if now >= self.next_check:
            self.check_callback()
            self.next_check = now + 1


def stop_process(process):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def stream_command(command, destination, *, stdin=None, timeout=3600, env=None):
    """Drain both pipes without buffering a dump in RAM or blocking on stderr."""
    guarded_input = isinstance(stdin, GuardedReader)
    if guarded_input:
        stdin.check()
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE if guarded_input else stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        start_new_session=True,
    )
    error = bytearray()
    deadline = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, "output")
            selector.register(process.stderr, selectors.EVENT_READ, "error")
            if guarded_input:
                os.set_blocking(process.stdin.fileno(), False)
                selector.register(process.stdin, selectors.EVENT_WRITE, "input")
            pending = b""
            output_buffer = bytearray()
            while selector.get_map():
                if guarded_input:
                    stdin.check()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise OpsError("Snapshot command timed out")
                for key, _ in selector.select(min(remaining, 1)):
                    if key.data == "input":
                        if not pending:
                            pending = stdin.file.read(COPY_CHUNK)
                        if not pending:
                            selector.unregister(process.stdin)
                            process.stdin.close()
                        else:
                            try:
                                written = os.write(key.fd, pending)
                                pending = pending[written:]
                            except BlockingIOError:
                                continue
                            except BrokenPipeError:
                                selector.unregister(process.stdin)
                                process.stdin.close()
                        continue
                    limit = COPY_CHUNK - len(output_buffer) if key.data == "output" else COPY_CHUNK
                    data = os.read(key.fd, limit)
                    if not data:
                        if key.data == "output" and output_buffer and destination is not None:
                            destination.write(output_buffer)
                            output_buffer.clear()
                        selector.unregister(key.fileobj)
                    elif key.data == "output":
                        if destination is not None:
                            output_buffer.extend(data)
                            if len(output_buffer) >= COPY_CHUNK:
                                destination.write(output_buffer)
                                output_buffer.clear()
                    elif len(error) < 500:
                        error.extend(data[: 500 - len(error)])
        process.wait(timeout=max(0.01, deadline - time.monotonic()))
        if process.returncode:
            raise OpsError(
                f"{Path(command[0]).name} failed: " + error.decode(errors="replace").strip()
            )
    finally:
        stop_process(process)
        process.stdout.close()
        process.stderr.close()
        if guarded_input:
            process.stdin.close()
    return b""


def tree_inventory(root):
    """Logical bytes plus conservative tar headers, including empty directories."""
    size = 0
    entries = 1
    for directory, names, files in os.walk(root, followlinks=False):
        entries += len(names) + len(files)
        for name in names + files:
            path = Path(directory) / name
            if path.is_symlink():
                raise OpsError("Snapshot source contains a symlink")
            if path.is_file():
                size += path.stat().st_size
    return size + entries * 8192
