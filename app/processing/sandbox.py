"""Linux child launcher: resource caps and SIGKILL when its exact parent dies."""

import ctypes
import os
import resource
import signal
import sys


def main():
    parent, memory, cpu, maximum, tool, *arguments = sys.argv[1:]
    # Popen invokes this fresh interpreter rather than using preexec_fn from a thread.
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
        raise OSError(ctypes.get_errno(), "Cannot establish parent death signal")
    if os.getppid() != int(parent):
        raise SystemExit(1)
    for limit, value in (
        (resource.RLIMIT_AS, int(memory)),
        (resource.RLIMIT_CPU, int(cpu)),
        (resource.RLIMIT_FSIZE, int(maximum)),
        (resource.RLIMIT_CORE, 0),
    ):
        resource.setrlimit(limit, (value, value))
    os.execvp(tool, [tool, *arguments])


if __name__ == "__main__":
    main()
