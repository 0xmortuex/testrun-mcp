"""The operations behind the MCP tools. No MCP types here, so tests can call it directly."""

from __future__ import annotations

import fnmatch
import json
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import node_runner, pytest_runner, unittest_runner
from .args import check_selector, parse_extra_args
from .config import Config
from .detect import choose_runner, how_it_runs, probe_python, scan
from .errors import TestrunError
from .model import Case, Report, build_result, case_dict
from .procs import ProcResult, run_process
from .pyenv import Interpreter, find_interpreter, missing_module_hint
from .trim import clip, clip_middle

MIN_TIMEOUT = 1
MAX_TIMEOUT = 3600
MAX_RERUN_IDS = 100
MAX_LISTED = 500
MAX_EXPLAIN_CHARS = 20000


@dataclass
class LastRun:
    runner: str
    python: str | None
    failed_ids: list[str]
    cases: dict[str, Case] = field(default_factory=dict)


LAST: dict[str, LastRun] = {}


def check_timeout(timeout: float) -> float:
    if not (MIN_TIMEOUT <= timeout <= MAX_TIMEOUT):
        raise TestrunError(f"timeout must be between {MIN_TIMEOUT} and {MAX_TIMEOUT} seconds")
    return timeout


def _interp(project: Path, python: str | None, runner: str) -> Interpreter | None:
    if runner in ("jest", "vitest"):
        return None
    return find_interpreter(project, python)


def execute(
    cfg: Config, project: Path, runner: str, python: str | None, targets: list[str],
    extra: list[str], timeout: float, tb: str = "short", showlocals: bool = False,
) -> tuple[Report, ProcResult, Interpreter | None, list[str]]:
    """Run `targets` (already validated) with `runner`; `extra` is already parsed argv."""
    interp = _interp(project, python, runner)
    if runner == "pytest":
        assert interp is not None
        report, proc, argv = pytest_runner.run_pytest(
            cfg, project, interp, targets, extra, timeout, tb, showlocals)
    elif runner == "unittest":
        assert interp is not None
        report, proc, argv = unittest_runner.run_unittest(
            cfg, project, interp, targets, extra, timeout)
    else:
        report, proc, argv = node_runner.run_node(cfg, project, runner, targets, extra, timeout)
    return report, proc, interp, argv


def ids_to_run(runner: str, ids: list[str]) -> tuple[list[str], list[str]]:
    """(targets, runner-specific extra argv) selecting exactly these test ids."""
    if runner in ("jest", "vitest"):
        return node_runner.targets_for_ids(runner, ids)
    return list(ids), []


def run_tests(
    cfg: Config, path: str, selector: str | None, timeout: float,
    extra_args: list[str] | None, python: str | None, runner: str | None,
) -> dict[str, Any]:
    project = cfg.resolve_project(path)
    check_timeout(timeout)
    chosen = choose_runner(project, runner)
    sel = check_selector(chosen, project, selector)
    extra = parse_extra_args(chosen, extra_args)
    report, proc, interp, argv = execute(
        cfg, project, chosen, python, [sel] if sel else [], extra, timeout)
    remember(project, chosen, python, report)
    result = build_result(report, proc, interp, project, argv)
    if result["outcome"] == "failed":
        result["next"] = "rerun_failed, or explain_failure(test_id) for the full traceback"
    return result


def remember(project: Path, runner: str, python: str | None, report: Report) -> None:
    LAST[str(project)] = LastRun(
        runner, python,
        [c.id for c in report.cases if c.outcome in ("failed", "error")],
        {c.id: c for c in report.cases},
    )


def rerun_failed(cfg: Config, path: str, timeout: float, python: str | None) -> dict[str, Any]:
    project = cfg.resolve_project(path)
    check_timeout(timeout)
    last = LAST.get(str(project))
    if last is None:
        raise TestrunError(
            f"no earlier run_tests result for {project} in this server session; call run_tests "
            "first (rerun_failed repeats only the failures of the last run)"
        )
    if not last.failed_ids:
        return {"outcome": "nothing_to_rerun", "runner": last.runner,
                "message": "the last run for this project had no failures or errors"}
    ids = last.failed_ids
    skipped_missing = 0
    if last.runner in ("pytest", "jest", "vitest"):
        # A failing file may have been deleted or renamed since; pytest would exit with
        # "file or directory not found" and run nothing. Drop those ids and say so.
        present = [i for i in ids if (project / i.split("::", 1)[0]).exists()]
        skipped_missing = len(ids) - len(present)
        ids = present
        if not ids:
            LAST[str(project)] = LastRun(last.runner, last.python, [])
            return {"outcome": "nothing_to_rerun", "runner": last.runner,
                    "message": "the files of the earlier failures no longer exist"}
    all_failed = len(ids)
    ids = ids[:MAX_RERUN_IDS]
    py = python if python is not None else last.python
    targets, extra = ids_to_run(last.runner, ids)
    report, proc, interp, argv = execute(cfg, project, last.runner, py, targets, extra, timeout)
    remember(project, last.runner, py, report)
    result = build_result(report, proc, interp, project, argv)
    result["rerun_ids"] = len(ids)
    if all_failed > len(ids):
        result.setdefault("notes", []).append(
            f"only the first {MAX_RERUN_IDS} of {all_failed} failures were rerun")
    if skipped_missing:
        result.setdefault("notes", []).append(
            f"{skipped_missing} earlier failure(s) skipped because their files no longer exist")
    return result


def list_tests(
    cfg: Config, path: str, selector: str | None, name_filter: str | None, timeout: float,
    python: str | None, runner: str | None,
) -> dict[str, Any]:
    project = cfg.resolve_project(path)
    check_timeout(timeout)
    chosen = choose_runner(project, runner)
    sel = check_selector(chosen, project, selector)
    if name_filter is not None and name_filter.startswith("-"):
        raise TestrunError("filter may not start with '-'")
    res: dict[str, Any] = {"runner": chosen, "project": str(project)}
    errors: list[str] = []
    note: str | None = None
    if chosen in ("jest", "vitest"):
        files = node_runner.list_files(cfg, project, chosen, [sel] if sel else [], timeout)
        ids = files
        note = ("jest/vitest are listed per test FILE; individual test names are only known "
                "after running them")
        if name_filter:
            ids = [f for f in files if name_filter.lower() in f.lower()]
    else:
        interp = find_interpreter(project, python)
        res["interpreter"] = f"{interp.display()} ({interp.source})"
        if chosen == "pytest":
            argv = [*interp.argv, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider",
                    "-o", "addopts=", "--continue-on-collection-errors"]
            if name_filter:
                argv += ["-k", name_filter]
            if sel:
                argv.append(sel)
            try:
                proc = run_process(argv, str(project), cfg.build_env(), timeout)
            except OSError as e:
                raise TestrunError(f"could not start {argv[0]!r}: {e}") from e
            text = proc.text()
            if proc.timed_out:
                raise TestrunError(
                    f"collection did not finish within {timeout}s (a module hangs at import time?)")
            if "No module named pytest" in text:
                raise TestrunError(missing_module_hint(interp, "pytest"))
            ids, errors = pytest_runner.parse_collect_only(text)
            if not ids and not errors and proc.returncode not in (0, 5):
                raise TestrunError(
                    "pytest could not collect tests (exit code "
                    f"{proc.returncode}). Output tail:\n{clip(text, 1500)}")
        else:
            targets = [sel] if sel else []
            extra = ["-k", name_filter] if name_filter else []
            ids, errors = unittest_runner.list_unittest(
                cfg, project, interp, targets, extra, timeout)
    res["count"] = len(ids)
    res["tests"] = ids[:MAX_LISTED]
    if len(ids) > MAX_LISTED:
        res["truncated"] = f"showing {MAX_LISTED} of {len(ids)}; narrow with selector or filter"
    if errors:
        res["collection_errors"] = [clip(e, 300) for e in errors[:10]]
    if note:
        res["note"] = note
    if not ids and not errors:
        res["note"] = "no tests collected"
    return res


def _letter(outcome: str) -> str:
    if outcome in ("passed", "xpassed"):
        return "P"
    if outcome in ("failed", "error"):
        return "F"
    return "S"


def flaky_check(
    cfg: Config, path: str, test_id: str, n: int, timeout: float, python: str | None,
    runner: str | None,
) -> dict[str, Any]:
    project = cfg.resolve_project(path)
    check_timeout(timeout)
    if not 2 <= n <= 20:
        raise TestrunError("n must be between 2 and 20")
    chosen = choose_runner(project, runner)
    check_selector(chosen, project, test_id)
    targets, extra = ids_to_run(chosen, [test_id])
    pattern = ""
    durations: list[float] = []
    first_failure: dict[str, Any] | None = None
    interp: Interpreter | None = None
    for i in range(n):
        report, proc, interp, _ = execute(cfg, project, chosen, python, targets, extra, timeout)
        durations.append(round(proc.duration, 2))
        if report.problem == "timeout":
            pattern += "T"
            continue
        if report.problem:
            raise TestrunError(
                f"run {i + 1}: the runner produced no report ({report.problem}, exit code "
                f"{report.exit_code}). Output tail:\n{clip(proc.tail or proc.head, 1500)}")
        match = [c for c in report.cases if c.id == test_id]
        if not match:
            coll = report.collection_errors()
            hint = (f" The file failed to import: {coll[0].message}" if coll else
                    " Use list_tests to see exact ids.")
            raise TestrunError(f"test id {test_id!r} was not found in the run.{hint}")
        letter = _letter(match[0].outcome)
        pattern += letter
        if letter == "F" and first_failure is None:
            first_failure = case_dict(match[0], project)
    runs = {k: pattern.count(k) for k in "PFST" if pattern.count(k)}
    if "P" in pattern and ("F" in pattern or "T" in pattern):
        verdict = "flaky"
    elif pattern.count("P") == n:
        verdict = "stable_pass"
    elif pattern.count("F") + pattern.count("T") == n:
        verdict = "consistently_failing"
    else:
        verdict = "inconclusive"
    res: dict[str, Any] = {
        "test_id": test_id, "runner": chosen, "runs": n, "pattern": pattern,
        "legend": "P pass, F fail, S skipped/xfail, T timeout", "counts": runs,
        "verdict": verdict, "run_seconds": durations,
        "interpreter": f"{interp.display()} ({interp.source})" if interp else "node",
    }
    if verdict == "flaky":
        res["flaky"] = True
        res["failure_rate"] = round((n - pattern.count("P")) / n, 2)
    if first_failure:
        res["first_failure"] = first_failure
    if verdict == "stable_pass":
        res["note"] = (f"passed {n}/{n} times in isolation; a test that is only flaky in the "
                       "full suite (order or parallelism) will not show here, so try run_tests "
                       "with the surrounding selector")
    return res


def explain_failure(
    cfg: Config, path: str, test_id: str, timeout: float, python: str | None, runner: str | None,
) -> dict[str, Any]:
    project = cfg.resolve_project(path)
    check_timeout(timeout)
    last = LAST.get(str(project))
    chosen = runner or (last.runner if last else choose_runner(project, None))
    check_selector(chosen, project, test_id)
    targets, extra = ids_to_run(chosen, [test_id])
    report, proc, interp, argv = execute(
        cfg, project, chosen, python, targets, extra, timeout, tb="long", showlocals=True)
    if report.problem:
        raise TestrunError(
            f"the runner produced no report ({report.problem}, exit code {report.exit_code}). "
            f"Output tail:\n{clip(proc.tail or proc.head, 1500)}")
    match = [c for c in report.cases if c.id == test_id]
    if not match:
        coll = report.collection_errors()
        if coll:
            match = coll
        else:
            raise TestrunError(
                f"test id {test_id!r} was not found. Use list_tests, or copy an id from "
                "run_tests output.")
    c = match[0]
    res: dict[str, Any] = {
        "id": c.id, "outcome": c.outcome, "runner": chosen,
        "interpreter": f"{interp.display()} ({interp.source})" if interp else "node",
    }
    if c.outcome not in ("failed", "error"):
        res["note"] = (
            "this test did not fail when run on its own. It may be flaky (try flaky_check) or "
            "depend on other tests or on ordering (run its whole file or module).")
        return res
    d = case_dict(c, project)
    res["message"] = c.message
    if "location" in d:
        res["location"] = d["location"]
    res["traceback"] = clip_middle(c.body, MAX_EXPLAIN_CHARS)
    if c.stdout:
        res["stdout"] = clip(c.stdout, 4000)
    if c.stderr:
        res["stderr"] = clip(c.stderr, 4000)
    if chosen == "pytest":
        res["locals_note"] = (
            "run with --tb=long --showlocals: local variables appear under each frame as "
            "`name = value`")
    return res


def _ranges(lines: list[int]) -> str:
    out: list[str] = []
    start = prev = None
    for n in sorted(lines):
        if start is None:
            start = prev = n
        elif prev is not None and n == prev + 1:
            prev = n
        else:
            out.append(str(start) if start == prev else f"{start}-{prev}")
            start = prev = n
    if start is not None:
        out.append(str(start) if start == prev else f"{start}-{prev}")
    return ", ".join(out)


MAX_COVERAGE_FILES = 30


def coverage_summary(
    cfg: Config, path: str, files: list[str] | None, selector: str | None, timeout: float,
    python: str | None, runner: str | None,
) -> dict[str, Any]:
    project = cfg.resolve_project(path)
    check_timeout(timeout)
    chosen = choose_runner(project, runner)
    if chosen not in ("pytest", "unittest"):
        raise TestrunError("coverage_summary supports Python projects (pytest or unittest) only")
    sel = check_selector(chosen, project, selector)
    interp = find_interpreter(project, python)
    env = cfg.build_env()
    with tempfile.TemporaryDirectory(prefix="testrun-cov-") as tmp:
        data = os.path.join(tmp, "cov.data")
        out_json = os.path.join(tmp, "cov.json")
        inner = (["-m", "pytest", "-q", "-p", "no:cacheprovider"] if chosen == "pytest"
                 else ["-m", "unittest"])
        argv = [*interp.argv, "-m", "coverage", "run", f"--data-file={data}", *inner]
        if sel:
            argv.append(sel)
        try:
            run = run_process(argv, str(project), env, timeout)
        except OSError as e:
            raise TestrunError(f"could not start {argv[0]!r}: {e}") from e
        text = run.text()
        if "No module named coverage" in text:
            raise TestrunError(missing_module_hint(interp, "coverage"))
        if run.timed_out:
            raise TestrunError(f"the test run under coverage did not finish within {timeout}s")
        if not os.path.exists(data):
            raise TestrunError(
                "coverage recorded no data. Output tail:\n" + clip(text, 1500))
        rep = run_process(
            [*interp.argv, "-m", "coverage", "json", f"--data-file={data}", "-o", out_json, "-q"],
            str(project), env, 120)
        if not os.path.exists(out_json):
            raise TestrunError("coverage json failed:\n" + clip(rep.text(), 1500))
        with open(out_json, encoding="utf-8") as fh:
            cov = json.load(fh)
    root = str(project).replace("\\", "/").rstrip("/") + "/"
    rows: list[dict[str, Any]] = []
    for name, info in cov["files"].items():
        n = name.replace("\\", "/")
        if os.path.isabs(name) or (len(n) > 1 and n[1] == ":"):
            if not n.lower().startswith(root.lower()):
                continue
            n = n[len(root):]
        s = info["summary"]
        rows.append({
            "file": n, "percent": round(s["percent_covered"], 1),
            "statements": s["num_statements"], "missed": s["missing_lines"],
            "uncovered_lines": _ranges(info["missing_lines"]),
        })
    if files:
        pats = [f.replace("\\", "/") for f in files]
        rows = [r for r in rows
                if any(fnmatch.fnmatch(r["file"], p) or r["file"].endswith(p.lstrip("./"))
                       for p in pats)]
        if not rows:
            raise TestrunError(
                f"none of {files} matched a measured file. Measured files: "
                + ", ".join(sorted({n for n in cov['files']}))[:600])
    rows.sort(key=lambda r: (r["percent"], r["file"]))
    res: dict[str, Any] = {
        "runner": chosen,
        "interpreter": f"{interp.display()} ({interp.source})",
        "tests_exit_code": run.returncode,
        "total_percent": round(cov["totals"]["percent_covered"], 1),
        "files": rows[:MAX_COVERAGE_FILES],
    }
    if len(rows) > MAX_COVERAGE_FILES:
        res["files_not_shown"] = len(rows) - MAX_COVERAGE_FILES
    if run.returncode not in (0, None):
        res["note"] = ("the tests did not all pass (exit code "
                       f"{run.returncode}); coverage covers what ran")
    return res


def detect(cfg: Config, path: str, python: str | None) -> dict[str, Any]:
    project = cfg.resolve_project(path)
    cands, info = scan(project)
    res: dict[str, Any] = {"project": str(project)}
    py_runners = [c for c in cands if c.runner in ("pytest", "unittest")]
    runners: list[dict[str, Any]] = []
    interp = find_interpreter(project, python) if (py_runners or python) else None
    probe = probe_python(cfg, project, interp) if interp else None
    for c in cands:
        entry: dict[str, Any] = {
            "runner": c.runner, "evidence": c.evidence,
            "how": how_it_runs(c.runner, interp if c.runner in ("pytest", "unittest") else None),
        }
        if c.runner == "pytest" and probe is not None:
            entry["ready"] = bool(probe.get("pytest"))
            if not entry["ready"]:
                entry["problem"] = "pytest is not installed for the interpreter above"
        elif c.runner == "unittest":
            entry["ready"] = bool(probe and probe.get("usable"))
        else:
            ok = (project / "node_modules" / c.runner).is_dir()
            entry["ready"] = ok and bool(shutil.which("node"))
            if not ok:
                entry["problem"] = f"node_modules/{c.runner} is missing: run npm ci / pnpm install"
            elif not entry["ready"]:
                entry["problem"] = "node is not on PATH"
        runners.append(entry)
    res["runners"] = runners
    res["primary"] = cands[0].runner if cands else None
    if probe is not None:
        res["python"] = probe
    res.update(info)
    if not cands:
        res["note"] = "no pytest/unittest/jest/vitest signs found"
    return res
