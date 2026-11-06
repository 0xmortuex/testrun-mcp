"""Run pytest and turn its JUnit XML into a Report."""

from __future__ import annotations

import os
import re
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from .config import Config
from .errors import TestrunError
from .model import Case, Report
from .procs import ProcResult, run_process
from .pyenv import Interpreter, missing_module_hint
from .trim import parse_traceback, split_captured, strip_banners

_RESULT_LINE = re.compile(
    r"^(?P<id>\S.*?::.*?)\s+(?P<res>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\b"
)
_STARTED_LINE = re.compile(r"^(?P<id>\S[^\s].*?::\S.*?)\s*$")
_XPASS_LINE = re.compile(r"^XPASS (?P<id>\S.*?)(?: - .*)?$", re.M)

# Exit codes: 0 ok, 1 failures, 2 interrupted, 3 internal error, 4 usage error, 5 no tests.


def base_argv(
    interp: Interpreter,
    junit: str,
    extra: list[str],
    tb: str = "short",
    showlocals: bool = False,
) -> list[str]:
    argv = [
        *interp.argv, "-m", "pytest",
        f"--junitxml={junit}",
        "-o", "junit_family=xunit1",  # xunit1 carries the file= attribute we need for ids
        "-o", "junit_logging=all",
        "-o", "junit_log_passing_tests=False",
        "-p", "no:cacheprovider",
        f"--tb={tb}",
        "-v", "-rxX",
        "--continue-on-collection-errors",
    ]
    if showlocals:
        argv.append("--showlocals")
    argv.extend(extra)
    return argv


def run_pytest(
    cfg: Config,
    project: Path,
    interp: Interpreter,
    targets: list[str],
    extra: list[str],
    timeout: float,
    tb: str = "short",
    showlocals: bool = False,
) -> tuple[Report, ProcResult, list[str]]:
    with tempfile.TemporaryDirectory(prefix="testrun-") as tmp:
        junit = os.path.join(tmp, "report.xml")
        argv = base_argv(interp, junit, extra, tb, showlocals) + targets
        proc = _start(argv, project, cfg, timeout)
        report = build_report(proc, junit, interp)
    # Report a compact command: the fixed reporting options are the same every time
    # (detect_runner shows them) and would only cost the agent tokens in every result.
    shown = [*interp.argv, "-m", "pytest", "<report options>", *extra, *targets]
    return report, proc, shown


def _start(argv: list[str], project: Path, cfg: Config, timeout: float) -> ProcResult:
    try:
        return run_process(argv, str(project), cfg.build_env(), timeout)
    except OSError as e:
        raise TestrunError(
            f"could not start {argv[0]!r}: {e}. Pass python= with the path of a working "
            "interpreter."
        ) from e


def build_report(proc: ProcResult, junit: str, interp: Interpreter) -> Report:
    text = proc.text()
    if proc.timed_out:
        started, counts = parse_verbose(proc.text())
        return Report("pytest", [], None, "timeout", started, counts)
    if not os.path.exists(junit):
        return no_report(proc, text, interp)
    cases = parse_junit(junit)
    xpass = set(m["id"] for m in _XPASS_LINE.finditer(text))
    for c in cases:
        if c.outcome == "passed" and c.id in xpass:
            c.outcome = "xpassed"
    report = Report("pytest", cases, proc.returncode)
    if proc.returncode == 3:
        report.problem = "internal_error"
        report.notes.append(
            "pytest hit an INTERNALERROR (exit code 3), for example a module calling "
            "sys.exit() at import time; see output_tail"
        )
    if proc.returncode == 5 and not cases:
        report.notes.append(
            "pytest collected no tests (exit code 5). Check the selector/-k/-m filters "
            "and that test files match test_*.py or *_test.py."
        )
    return report


def no_report(proc: ProcResult, text: str, interp: Interpreter) -> Report:
    if "No module named pytest" in text:
        raise TestrunError(missing_module_hint(interp, "pytest"))
    started, counts = parse_verbose(text)
    code = proc.returncode
    if code in (2, 3, 4, 5):
        # pytest itself reported a problem before/while collecting (bad conftest,
        # usage error, internal error) and wrote no XML.
        if code == 5:
            r = Report("pytest", [], code)
            r.notes.append("pytest collected no tests (exit code 5)")
            return r
        return Report("pytest", [], code, "internal_error" if code == 3 else "startup_error",
                      None, counts)
    return Report("pytest", [], code, "crashed", started, counts)


def parse_verbose(text: str) -> tuple[str | None, dict[str, int]]:
    """From -v output: counts of finished tests, and the test that had started
    but not finished (the one that hung or killed the process)."""
    counts: dict[str, int] = {}
    last_line = ""
    for line in text.splitlines():
        if not line.strip():
            continue
        last_line = line
        m = _RESULT_LINE.match(line)
        if m:
            key = m["res"].lower()
            counts[key] = counts.get(key, 0) + 1
    started = None
    if "::" in last_line and not _RESULT_LINE.match(last_line):
        m2 = _STARTED_LINE.match(last_line)
        if m2:
            started = m2["id"].strip()
    return started, counts


def parse_junit(path: str) -> list[Case]:
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as e:
        raise TestrunError(f"pytest wrote a JUnit XML file that could not be parsed: {e}") from e
    cases: list[Case] = []
    for tc in root.iter("testcase"):
        cases.append(_case(tc))
    return cases


def node_id(file: str, classname: str, name: str) -> str:
    file = file.replace("\\", "/")
    if not file:
        return f"{classname}::{name}" if classname else name
    module = file[:-3].replace("/", ".") if file.endswith(".py") else file
    if classname == module or not classname:
        return f"{file}::{name}"
    if classname.startswith(module + "."):
        classes = classname[len(module) + 1:].split(".")
        return "::".join([file, *classes, name])
    return f"{file}::{name}"


def _case(tc: ET.Element) -> Case:
    file = tc.get("file", "")
    name = tc.get("name", "")
    classname = tc.get("classname", "")
    duration = float(tc.get("time", "0") or 0)
    fail = tc.find("failure")
    err = tc.find("error")
    skip = tc.find("skipped")
    out = strip_banners(tc.findtext("system-out", ""))
    errtxt = strip_banners(tc.findtext("system-err", ""))
    if err is not None and err.get("message") == "collection failure":
        body = err.text or ""
        frames, e_lines = parse_traceback(body)
        message = e_lines[-1] if e_lines else "collection failure"
        return Case(
            file.replace("\\", "/") or name, "error", duration, message, frames, e_lines,
            body, kind="collection",
        )
    cid = node_id(file, classname, name)
    if fail is not None or err is not None:
        el = fail if fail is not None else err
        assert el is not None
        body_all = el.text or ""
        body, _ = split_captured(body_all)
        frames, e_lines = parse_traceback(body)
        outcome = "failed" if fail is not None else "error"
        message = (el.get("message") or (e_lines[0] if e_lines else "")).strip()
        return Case(cid, outcome, duration, message, frames, e_lines, body_all, out, errtxt)
    if skip is not None:
        typ = skip.get("type", "")
        outcome = "xfailed" if typ == "pytest.xfail" else "skipped"
        reason = skip.get("message", "") or ""
        return Case(cid, outcome, duration, reason=reason)
    return Case(cid, "passed", duration)


def parse_collect_only(text: str) -> tuple[list[str], list[str]]:
    """From `--collect-only -q`: (node ids, collection error lines)."""
    ids: list[str] = []
    errors: list[str] = []
    for line in text.splitlines():
        s = line.rstrip()
        if s.startswith("ERROR "):
            errors.append(s[6:])
        elif "::" in s and not s.startswith((" ", "=", "-", "!")) and " " not in s.split("::")[0]:
            ids.append(s)
    return ids, errors
