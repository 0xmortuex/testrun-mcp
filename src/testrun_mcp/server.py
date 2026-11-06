"""testrun-mcp: run a project's tests and return small, structured results.

WARNING: running tests executes the project's code (and its conftest.py, plugins,
fixtures, import-time code) with your user's permissions. Only point this server
at code you would run yourself. See the README security section.
"""

from __future__ import annotations

import argparse
import functools
from collections.abc import Callable
from typing import Any, TypeVar

import anyio.to_thread
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import config as cfgmod
from . import service
from .errors import TestrunError

mcp = MCPServer(
    "testrun",
    instructions=(
        "Run a project's tests and get a small structured result instead of a long log. "
        "NOTE: this executes the project's code. Typical loop: detect_runner(path) once, "
        "run_tests(path), then for failures explain_failure(path, test_id), fix, "
        "rerun_failed(path); use flaky_check for tests that fail intermittently and "
        "coverage_summary to find untested lines. `path` is the project root and should be "
        "absolute. Every result names the interpreter that was used; if pytest is missing "
        "there, pass python= (the project's venv interpreter, or 'uv')."
    ),
)

F = TypeVar("F", bound=Callable[..., Any])


def tool(fn: F) -> F:
    """Register fn as an MCP tool. It runs in a worker thread (tests can take minutes
    and must not block the protocol loop); expected errors become ToolError so the
    message reaches the client."""

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))
        except TestrunError as e:
            raise ToolError(str(e)) from e

    mcp.tool()(wrapper)
    return fn




def _cfg() -> cfgmod.Config:
    return cfgmod.CONFIG


@tool
def detect_runner(path: str = ".", python: str | None = None) -> dict[str, Any]:
    """Detect which test runner a project uses, without running any of its tests.

    Looks for pytest (config, conftest.py, dependencies), unittest, jest and vitest
    (package.json, config files). For each candidate: the evidence, the exact way it would
    be run, and whether it is ready (pytest importable, node_modules present). For Python it
    also reports which interpreter would be used (project .venv, else the server's own) and
    the versions of pytest/coverage found there. Returns test directories too.

    path: project root (absolute is safest). python: optional interpreter path, or 'uv'.
    """
    return service.detect(_cfg(), path, python)


@tool
def run_tests(
    path: str = ".",
    selector: str | None = None,
    timeout: float = 300,
    extra_args: list[str] | None = None,
    python: str | None = None,
    runner: str | None = None,
) -> dict[str, Any]:
    """Run the tests and return counts plus only the useful details of each failure.

    EXECUTES THE PROJECT'S CODE. Result: outcome (passed | failed | no_tests | timeout |
    crashed | startup_error), summary counts (passed/failed/error/skipped/xfailed/xpassed),
    failures (id, message, file:line of the innermost project frame, the relevant traceback
    frames only, trimmed stdout/stderr), collection_errors listed separately from failures
    (import errors never hide as test failures), the 5 slowest tests, and which interpreter
    was used. On timeout or crash it names the test that was running. The whole process tree
    is killed at the timeout.

    selector: a file, directory or node id (pytest: tests/test_a.py::TestX::test_y;
      unittest: dotted name; jest/vitest: file path pattern). Default: everything.
    timeout: seconds, 1-3600. extra_args: only allow-listed flags, e.g. ["-x"],
      ["-k", "login"], ["-m", "not slow"], ["--maxfail=3"]; anything else is
      refused. python: interpreter path or 'uv'. runner: force pytest/unittest/jest/vitest
      (default: auto-detected, see detect_runner).
    """
    return service.run_tests(_cfg(), path, selector, timeout, extra_args, python, runner)


@tool
def rerun_failed(path: str = ".", timeout: float = 300, python: str | None = None) -> dict[str, Any]:
    """Re-run only the tests that failed or errored in the last run_tests/rerun_failed for
    this project (same server session). Returns the same structure as run_tests, plus how
    many ids were rerun (at most 100). The failure list is replaced by what still fails."""
    return service.rerun_failed(_cfg(), path, timeout, python)


@tool
def list_tests(
    path: str = ".",
    selector: str | None = None,
    filter: str | None = None,
    timeout: float = 120,
    python: str | None = None,
    runner: str | None = None,
) -> dict[str, Any]:
    """List the tests without running them (collect-only). Returns ids (max 500) and any
    collection errors (import errors in test modules or conftest.py), reported separately.

    filter: pytest -k expression / unittest name pattern / substring of a jest or vitest file
    name. For jest and vitest only test FILES can be listed. Use the ids as run_tests
    selectors and as flaky_check / explain_failure test_id."""
    return service.list_tests(_cfg(), path, selector, filter, timeout, python, runner)


@tool
def flaky_check(
    path: str,
    test_id: str,
    n: int = 5,
    timeout: float = 60,
    python: str | None = None,
    runner: str | None = None,
) -> dict[str, Any]:
    """Run ONE test n times (2-20, each a fresh process) and report the pass/fail pattern.

    Returns pattern like "PPFPP" (P pass, F fail, S skipped, T timeout), counts, a verdict
    (flaky | stable_pass | consistently_failing | inconclusive), the failure rate, and the
    first failure's details. Worst case it takes n x timeout seconds. A test that is only
    flaky when run with others (ordering, parallelism, shared state) can pass here; check
    that with run_tests on the surrounding file.

    test_id: exact id from run_tests/list_tests (pytest: path::Class::test[param];
    jest/vitest: path/file.test.js::full test name; unittest: dotted name)."""
    return service.flaky_check(_cfg(), path, test_id, n, timeout, python, runner)


@tool
def explain_failure(
    path: str,
    test_id: str,
    timeout: float = 120,
    python: str | None = None,
    runner: str | None = None,
) -> dict[str, Any]:
    """Full detail for one failing test: re-runs just that test with the long traceback and
    (for pytest) --showlocals, and returns the complete traceback text (capped at 20k
    chars) with local variable values, the assertion message, and captured stdout/stderr.
    If the test passes when run alone, it says so (flaky or order-dependent)."""
    return service.explain_failure(_cfg(), path, test_id, timeout, python, runner)


@tool
def coverage_summary(
    path: str = ".",
    files: list[str] | None = None,
    selector: str | None = None,
    timeout: float = 600,
    python: str | None = None,
    runner: str | None = None,
) -> dict[str, Any]:
    """Run the Python tests under coverage.py and report per-file percent and the uncovered
    line ranges (e.g. "12-15, 20"). Needs `coverage` installed in the project's interpreter
    (the error says which). Data goes to a temp dir; nothing is written into the project.

    files: optional list of paths or globs relative to the project (src/app.py, src/*.py) to
    report; default is the 30 least-covered project files plus the total percent.
    selector: run only part of the suite. Python projects only."""
    return service.coverage_summary(_cfg(), path, files, selector, timeout, python, runner)


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="testrun-mcp",
        description="MCP server that runs a project's tests and returns structured results. "
        "WARNING: it executes the project's code.",
    )
    ap.add_argument(
        "--dir", metavar="DIR",
        help="sandbox: only projects inside DIR may be tested (a path guard, not a security boundary)",
    )
    ap.add_argument(
        "--allow-env", metavar="NAME", action="append", default=[],
        help="pass this environment variable through to test processes (repeatable)",
    )
    ap.add_argument(
        "--inherit-env", action="store_true",
        help="do NOT scrub the environment: test processes see every variable, secrets included",
    )
    ns = ap.parse_args()
    cfgmod.set_config(
        cfgmod.Config(
            sandbox=cfgmod.parse_sandbox(ns.dir) if ns.dir else None,
            allow_env=frozenset(n.upper() for n in ns.allow_env),
            scrub_env=not ns.inherit_env,
        )
    )
    mcp.run()


if __name__ == "__main__":
    main()
