"""Run jest or vitest (through node, never through npx/shell) and read their JSON."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from .config import Config
from .errors import TestrunError
from .model import Case, Report
from .procs import ProcResult, run_process
from .trim import Frame

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_AT = re.compile(r"^\s*at (?:(?P<fn>.+?) \()?(?P<file>[^()]+?):(?P<line>\d+):\d+\)?\s*$")

ENTRY = {
    "jest": ("jest", "bin", "jest.js"),
    "vitest": ("vitest", "vitest.mjs"),
}


def find_node() -> str:
    node = shutil.which("node")
    if node is None:
        raise TestrunError(
            "node is not on PATH, so jest/vitest cannot run. Install Node.js (and make sure "
            "`node` is on the PATH of the process that started testrun-mcp)."
        )
    return node


def find_entry(project: Path, runner: str) -> Path:
    entry = project.joinpath("node_modules", *ENTRY[runner])
    if not entry.is_file():
        raise TestrunError(
            f"{runner} is not installed in {project}: {entry} does not exist. Run "
            f"`npm ci` (or `pnpm install`) in the project first; testrun-mcp never installs packages."
        )
    return entry


def _start(argv: list[str], project: Path, cfg: Config, timeout: float) -> ProcResult:
    try:
        return run_process(argv, str(project), cfg.build_env(), timeout)
    except OSError as e:
        raise TestrunError(f"could not start {argv[0]!r}: {e}") from e


def run_node(
    cfg: Config, project: Path, runner: str, patterns: list[str], extra: list[str],
    timeout: float,
) -> tuple[Report, ProcResult, list[str]]:
    node = find_node()
    entry = find_entry(project, runner)
    with tempfile.TemporaryDirectory(prefix="testrun-") as tmp:
        out = os.path.join(tmp, "report.json")
        if runner == "jest":
            argv = [node, str(entry), "--json", f"--outputFile={out}", "--ci"]
        else:
            argv = [node, str(entry), "run", "--reporter=json", f"--outputFile={out}"]
        argv += extra + patterns
        proc = _start(argv, project, cfg, timeout)
        shown = [a if not a.startswith("--outputFile=") else "--outputFile=<tmp>" for a in argv]
        if proc.timed_out:
            return Report(runner, [], None, "timeout"), proc, shown
        if not os.path.exists(out):
            code = proc.returncode
            return Report(runner, [], code, "startup_error" if code else "crashed"), proc, shown
        with open(out, encoding="utf-8") as fh:
            try:
                data = json.load(fh)
            except json.JSONDecodeError as e:
                raise TestrunError(f"{runner} wrote an unreadable JSON report: {e}") from e
    cases = parse_results(data, project)
    return Report(runner, cases, proc.returncode), proc, shown


def _rel(name: str, project: Path) -> str:
    n = name.replace("\\", "/")
    root = str(project).replace("\\", "/").rstrip("/") + "/"
    return n[len(root):] if n.lower().startswith(root.lower()) else n


def parse_frames(text: str) -> list[Frame]:
    frames: list[Frame] = []
    for line in text.splitlines():
        m = _AT.match(line)
        if m:
            file = unquote(m["file"].replace("file:///", "").replace("file://", ""))
            frames.append(Frame(file, int(m["line"]), m["fn"] or "<anonymous>", ""))
    frames.reverse()  # node prints innermost first; Frame lists are outermost first
    return frames


def parse_results(data: dict[str, Any], project: Path) -> list[Case]:
    cases: list[Case] = []
    for suite in data.get("testResults", []):
        file = _rel(suite.get("name", ""), project)
        results = suite.get("assertionResults", [])
        if not results and suite.get("status") == "failed":
            msg = _ANSI.sub("", suite.get("message", ""))
            lines = [ln.strip() for ln in msg.splitlines() if ln.strip()]
            lines = [ln for ln in lines if "Test suite failed to run" not in ln]
            cases.append(Case(
                file, "error", 0.0, lines[0][:400] if lines else "suite failed to run",
                parse_frames(msg), lines[:12], msg, kind="collection",
            ))
            continue
        for r in results:
            status = r.get("status")
            cid = f"{file}::{r.get('fullName') or r.get('title', '')}"
            dur = float(r.get("duration") or 0) / 1000.0
            if status == "passed":
                cases.append(Case(cid, "passed", dur))
            elif status == "failed":
                raw = "\n".join(r.get("failureMessages") or [])
                msg = _ANSI.sub("", raw)
                body_lines = msg.splitlines()
                head = [ln for ln in body_lines
                        if not _AT.match(ln) and not ln.strip().startswith("at ")]
                first = next((ln.strip() for ln in head if ln.strip()), "failed")
                cases.append(Case(cid, "failed", dur, first, parse_frames(msg), [ln.strip() for ln in head if ln.strip()][:12], msg))
            else:  # pending, todo, skipped, disabled
                cases.append(Case(cid, "skipped", dur, reason=str(status)))
    return cases


def targets_for_ids(runner: str, ids: list[str]) -> tuple[list[str], list[str]]:
    """(positional file patterns, extra argv) that select these tests.

    An id is 'file::full test name'. An id with no '::' is a whole file (a suite
    that failed to load has no test names); if any are present the files are rerun
    whole because -t would filter their tests out.
    """
    files: list[str] = []
    names: list[str] = []
    whole_file = False
    for i in ids:
        f, sep, n = i.partition("::")
        if f not in files:
            files.append(f)
        if sep:
            names.append("^" + re.escape(n) + "$")
        else:
            whole_file = True
    # jest treats file arguments as regexes, vitest as plain substrings.
    patterns = [re.escape(f) if runner == "jest" else f for f in files]
    return patterns, [] if whole_file or not names else ["-t", "|".join(names)]


def list_files(cfg: Config, project: Path, runner: str, patterns: list[str], timeout: float) -> list[str]:
    node = find_node()
    entry = find_entry(project, runner)
    if runner == "jest":
        argv = [node, str(entry), "--listTests", *patterns]
    else:
        argv = [node, str(entry), "list", "--filesOnly", *patterns]
    proc = _start(argv, project, cfg, timeout)
    if proc.timed_out or proc.returncode != 0:
        raise TestrunError(f"{runner} could not list tests. Output tail:\n{proc.text()[-1500:]}")
    return [_rel(ln.strip(), project) for ln in proc.text().splitlines()
            if ln.strip() and not ln.startswith(("Run ", "Vitest", " "))]
