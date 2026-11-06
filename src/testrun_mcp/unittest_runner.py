"""Run stdlib unittest through a bundled helper script and read its JSON report."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from .config import Config
from .errors import TestrunError
from .model import Case, Report
from .procs import ProcResult, run_process
from .pyenv import Interpreter
from .trim import Frame

HELPER = str(Path(__file__).parent / "_helper" / "testrun_unittest_helper.py")


def _run(
    cfg: Config, project: Path, interp: Interpreter, extra: list[str], targets: list[str],
    timeout: float, list_only: bool,
) -> tuple[dict[str, Any] | None, ProcResult, list[str]]:
    with tempfile.TemporaryDirectory(prefix="testrun-") as tmp:
        out = os.path.join(tmp, "report.json")
        argv = [*interp.argv, HELPER, "--out", out]
        if list_only:
            argv.append("--list")
        argv += extra + targets
        try:
            proc = run_process(argv, str(project), cfg.build_env(), timeout)
        except OSError as e:
            raise TestrunError(f"could not start {argv[0]!r}: {e}") from e
        data = None
        if os.path.exists(out):
            with open(out, encoding="utf-8") as fh:
                data = json.load(fh)
    return data, proc, argv


def split_buffered(body: str) -> tuple[str, str, str]:
    """unittest's buffer=True appends 'Stdout:' / 'Stderr:' sections to the traceback."""
    out = err = ""
    head, sep, tail = body.partition("\nStderr:\n")
    if sep:
        body, err = head, tail.strip()
    head, sep, tail = body.partition("\nStdout:\n")
    if sep:
        body, out = head, tail.strip()
    return body, out, err


def run_unittest(
    cfg: Config, project: Path, interp: Interpreter, targets: list[str], extra: list[str],
    timeout: float,
) -> tuple[Report, ProcResult, list[str]]:
    data, proc, argv = _run(cfg, project, interp, extra, targets, timeout, False)
    if proc.timed_out:
        text = proc.text()
        running = None
        lines = [ln for ln in text.splitlines() if ln.strip()]
        if lines and lines[-1].rstrip().endswith("..."):
            m = re.search(r"\(([^)]*)\)\s*\.\.\.\s*$", lines[-1])
            if m:
                running = m.group(1)
                name = lines[-1].split(" (", 1)[0].strip()
                # Python 3.10 prints "test_x (pkg.mod.Class)"; 3.11+ prints
                # "test_x (pkg.mod.Class.test_x)". Normalise to the full id.
                if not running.endswith("." + name):
                    running = f"{running}.{name}"
            else:
                running = lines[-1].rstrip(" .")
        return Report("unittest", [], None, "timeout", running), proc, argv
    if data is None:
        problem = "startup_error" if proc.returncode in (1, 2) else "crashed"
        return Report("unittest", [], proc.returncode, problem), proc, argv
    cases: list[Case] = []
    for r in data["cases"]:
        frames = [Frame(f["file"], f["line"], f["func"], f["code"]) for f in r.get("frames", [])]
        body, stdout, stderr = split_buffered(r.get("body", ""))
        msg = r.get("message", "") or r.get("reason", "")
        cases.append(
            Case(
                r["id"], r["outcome"], r["time"], msg, frames, [msg] if msg else [], body,
                stdout=stdout, stderr=stderr, reason=r.get("reason", ""),
                kind="collection" if r.get("collection") else "test",
            )
        )
    return Report("unittest", cases, proc.returncode), proc, argv


def list_unittest(
    cfg: Config, project: Path, interp: Interpreter, targets: list[str], extra: list[str],
    timeout: float,
) -> tuple[list[str], list[str]]:
    data, proc, _ = _run(cfg, project, interp, extra, targets, timeout, True)
    if data is None:
        raise TestrunError(
            "unittest could not list tests (the helper wrote no report). Output tail:\n"
            + proc.text()[-1500:]
        )
    ids = [t["id"] for t in data["tests"] if not t["collection"]]
    errors = [t["id"] for t in data["tests"] if t["collection"]]
    return ids, errors
