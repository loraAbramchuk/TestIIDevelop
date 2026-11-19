import io
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from scripts import ops_storage as storage


def test_stream_drains_full_stderr_pipe_and_bounds_error_memory(tmp_path):
    output = io.BytesIO()
    command = [
        sys.executable,
        "-c",
        "import sys;sys.stderr.buffer.write(b'e'*1048576);"
        "sys.stderr.flush();sys.stdout.buffer.write(b'x'*2097152);sys.exit(7)",
    ]
    with pytest.raises(storage.OpsError) as error:
        storage.stream_command(
            command, storage.GuardedWriter(output, storage.SpaceBudget(tmp_path, 0)), timeout=10
        )
    assert output.getvalue() == b"x" * 2097152
    assert len(str(error.value)) < 600


def test_guarded_input_and_output_do_not_deadlock_on_pipe_backpressure(tmp_path):
    payload = b"input" * 400000
    output = io.BytesIO()
    checks = []
    command = [
        sys.executable,
        "-c",
        "import sys;sys.stderr.buffer.write(b'e'*1048576);sys.stderr.flush();"
        "sys.stdout.buffer.write(b'o'*1048576);sys.stdout.flush();"
        "data=sys.stdin.buffer.read();sys.stdout.buffer.write(data)",
    ]
    storage.stream_command(
        command,
        storage.GuardedWriter(output, storage.SpaceBudget(tmp_path, 0)),
        stdin=storage.GuardedReader(io.BytesIO(payload), lambda: checks.append(True)),
        timeout=10,
    )
    assert output.getvalue() == b"o" * 1048576 + payload
    assert checks


def test_reserve_failure_terminates_child_and_keeps_partial_output_bounded(tmp_path, monkeypatch):
    child = []
    real_popen = subprocess.Popen

    def record(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        child.append(process)
        return process

    monkeypatch.setattr(storage.subprocess, "Popen", record)
    remaining = iter([1024**3, 0])
    monkeypatch.setattr(
        storage.shutil, "disk_usage", lambda p: SimpleNamespace(free=next(remaining))
    )
    output = io.BytesIO()
    command = [
        sys.executable,
        "-c",
        "import sys,time;sys.stdout.buffer.write(b'x'*10485760);time.sleep(30)",
    ]
    with pytest.raises(storage.OpsError, match="reserve"):
        storage.stream_command(
            command, storage.GuardedWriter(output, storage.SpaceBudget(tmp_path, 1)), timeout=10
        )
    assert len(output.getvalue()) <= storage.COPY_CHUNK
    assert child[0].poll() is not None
    with pytest.raises(ProcessLookupError):
        os.kill(child[0].pid, 0)


def test_stream_timeout_kills_process_without_waiting_for_full_sleep(tmp_path):
    with pytest.raises(storage.OpsError, match="timed out"):
        storage.stream_command(
            [sys.executable, "-c", "import time;time.sleep(30)"], None, timeout=0.1
        )


def test_target_guard_stops_restore_before_spawning_process(tmp_path, monkeypatch):
    monkeypatch.setattr(
        storage.subprocess, "Popen", lambda *a, **kw: pytest.fail("must not spawn import")
    )

    def no_space():
        raise storage.OpsError("Docker volume reserve reached")

    with pytest.raises(storage.OpsError, match="reserve"):
        storage.stream_command(
            ["unused"], None, stdin=storage.GuardedReader(io.BytesIO(), no_space)
        )
