"""Run a command safely: no shell, stdin closed, hard timeout, whole-tree kill,
bounded output.

The kill is the important part. A test that spawns a helper process (a dev
server, a worker pool, a `sleep 9999`) must not outlive the run:

* Windows: the child is put in a Job Object with KILL_ON_JOB_CLOSE, so
  TerminateJobObject (or closing the handle) kills every descendant, including
  ones whose parent already exited.
* POSIX: the child starts a new session (its own process group) and the group
  gets SIGKILL. A descendant that deliberately calls setsid() escapes this.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any

HEAD_BYTES = 32 * 1024
TAIL_BYTES = 96 * 1024
# After the direct child exits, how long to wait for the output pipe to close
# before concluding that a descendant is holding it open and killing the tree.
PIPE_DRAIN_SECONDS = 2.0


@dataclass
class ProcResult:
    returncode: int | None
    timed_out: bool
    duration: float
    head: str
    tail: str
    total_bytes: int
    dropped_bytes: int
    leftover_killed: bool = False
    argv: list[str] = field(default_factory=list)

    @property
    def truncated(self) -> bool:
        return self.total_bytes > len(self.head.encode()) + len(self.tail.encode())

    def text(self) -> str:
        """All captured output; the middle is replaced by a marker when capped."""
        if not self.truncated:
            return self.head + self.tail if self.tail else self.head
        dropped = self.total_bytes - HEAD_BYTES - TAIL_BYTES
        return f"{self.head}\n[... {dropped} bytes of output dropped ...]\n{self.tail}"


class _Capture:
    """Keep the first HEAD_BYTES and last TAIL_BYTES of a stream, count the rest."""

    def __init__(self) -> None:
        self.head = bytearray()
        self.tail = bytearray()
        self.total = 0

    def feed(self, chunk: bytes) -> None:
        self.total += len(chunk)
        room = HEAD_BYTES - len(self.head)
        if room > 0:
            self.head += chunk[:room]
            chunk = chunk[room:]
        if chunk:
            self.tail += chunk
            if len(self.tail) > TAIL_BYTES:
                del self.tail[: len(self.tail) - TAIL_BYTES]


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _JobObjectExtendedLimitInformation = 9
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000

    class _BasicLimit(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _IoCounters(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint64) for n in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class _ExtendedLimit(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimit),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.CreateJobObjectW.restype = wintypes.HANDLE
    _k32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    _k32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    _k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]


class TreeKiller:
    """Owns the process tree of one child and can kill all of it."""

    def __init__(self, proc: subprocess.Popen[bytes]) -> None:
        self._proc = proc
        self._job: Any = None
        if sys.platform == "win32":
            job = _k32.CreateJobObjectW(None, None)
            if not job:
                raise OSError(ctypes.get_last_error(), "CreateJobObject failed")
            info = _ExtendedLimit()
            info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not _k32.SetInformationJobObject(
                job, _JobObjectExtendedLimitInformation,
                ctypes.byref(info), ctypes.sizeof(info),
            ):
                raise OSError(ctypes.get_last_error(), "SetInformationJobObject failed")
            handle = int(proc._handle)  # type: ignore[attr-defined]
            if not _k32.AssignProcessToJobObject(job, handle):
                raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject failed")
            self._job = job

    def kill(self) -> None:
        if sys.platform == "win32":
            if self._job:
                _k32.TerminateJobObject(self._job, 1)
        else:
            try:
                os.killpg(self._proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError:
                pass

    def close(self) -> None:
        if sys.platform == "win32" and self._job:
            _k32.CloseHandle(self._job)
            self._job = None


def _pump(stream: Any, cap: _Capture) -> None:
    try:
        while True:
            chunk = stream.read1(65536)
            if not chunk:
                return
            cap.feed(chunk)
    except (OSError, ValueError):
        # Pipe closed under us after the tree was killed.
        return


def run_process(
    argv: list[str],
    cwd: str,
    env: dict[str, str],
    timeout: float,
) -> ProcResult:
    """Run argv (never through a shell) and return its merged stdout+stderr.

    Raises OSError if the program cannot be started (the caller turns that into
    an actionable message).
    """
    popen_kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    else:
        popen_kwargs["start_new_session"] = True
    start = time.monotonic()
    proc = subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        shell=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        **popen_kwargs,
    )
    killer = TreeKiller(proc)
    cap = _Capture()
    assert proc.stdout is not None
    reader = threading.Thread(target=_pump, args=(proc.stdout, cap), daemon=True)
    reader.start()
    timed_out = False
    leftover = False
    try:
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            killer.kill()
            proc.wait()
        reader.join(PIPE_DRAIN_SECONDS)
        if reader.is_alive():
            # A descendant still holds the pipe open: it outlived the test run.
            leftover = True
        # Always sweep the tree: removes daemons the tests left behind.
        killer.kill()
        reader.join(PIPE_DRAIN_SECONDS)
    finally:
        killer.close()
        try:
            proc.stdout.close()
        except OSError:
            pass
    duration = time.monotonic() - start
    return ProcResult(
        returncode=None if timed_out else proc.returncode,
        timed_out=timed_out,
        duration=duration,
        head=bytes(cap.head).decode("utf-8", errors="replace"),
        tail=bytes(cap.tail).decode("utf-8", errors="replace"),
        total_bytes=cap.total,
        dropped_bytes=cap.total - len(cap.head) - len(cap.tail),
        leftover_killed=leftover,
        argv=argv,
    )
