"""Shared helpers for the tests (kept out of conftest so they can be imported)."""

from __future__ import annotations

import subprocess
import sys
import textwrap
import time
from pathlib import Path


def write_project(root: Path, files: dict[str, str]) -> Path:
    """Write files (dedented, UTF-8, LF newlines) under root and return root."""
    root.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(textwrap.dedent(text).lstrip("\n"))
    return root


def pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        import ctypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = ctypes.c_void_p
        handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not k32.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code)):
                return False
            return code.value == 259  # STILL_ACTIVE
        finally:
            k32.CloseHandle(ctypes.c_void_p(handle))
    out = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True
    ).stdout.strip()
    return bool(out) and not out.startswith("Z")


def wait_dead(pid: int, seconds: float = 15.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if not pid_alive(pid):
            return True
        time.sleep(0.1)
    return not pid_alive(pid)


def wait_file(path: Path, seconds: float = 15.0) -> str:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if path.exists() and path.read_text(encoding="utf-8").strip():
            return path.read_text(encoding="utf-8").strip()
        time.sleep(0.05)
    raise AssertionError(f"{path} was never written")
