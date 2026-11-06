"""Runner-independent result model and the compact JSON-able rendering."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .procs import ProcResult
from .pyenv import Interpreter
from .trim import Frame, clip, clip_middle, relevant_frames

MAX_FAILURES = 20
MAX_COLLECTION_ERRORS = 10
MAX_OTHER_IDS = 50
SLOWEST = 5

OUTCOMES = ("passed", "failed", "error", "skipped", "xfailed", "xpassed")


@dataclass
class Case:
    id: str
    outcome: str  # one of OUTCOMES
    duration: float = 0.0
    message: str = ""
    frames: list[Frame] = field(default_factory=list)  # all frames, outermost first
    e_lines: list[str] = field(default_factory=list)
    body: str = ""  # full failure text as the runner printed it
    stdout: str = ""
    stderr: str = ""
    reason: str = ""  # skip / xfail reason
    kind: str = "test"  # "test" or "collection"


@dataclass
class Report:
    runner: str
    cases: list[Case]
    exit_code: int | None
    # Set when the run produced no machine-readable report.
    problem: str | None = None  # "timeout" | "crashed" | "startup_error"
    hung_test: str | None = None
    finished_before_stop: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def failing(self) -> list[Case]:
        return [c for c in self.cases if c.kind == "test" and c.outcome in ("failed", "error")]

    def collection_errors(self) -> list[Case]:
        return [c for c in self.cases if c.kind == "collection"]


def _rel(p: str, project: Path) -> str:
    q = p.replace("\\", "/")
    root = str(project).replace("\\", "/").rstrip("/") + "/"
    if q.lower().startswith(root.lower()):
        return q[len(root):]
    return q


def case_dict(c: Case, project: Path, with_output: bool = True) -> dict[str, Any]:
    own = [Frame(_rel(f.file, project), f.line, f.func, f.code) for f in c.frames]
    frames, dropped = relevant_frames(own) if own else ([], 0)
    d: dict[str, Any] = {"id": c.id, "kind": "error" if c.outcome == "error" else "failure"}
    if c.kind == "collection":
        d["kind"] = "collection_error"
    d["message"] = clip_middle(c.message, 600)
    if frames:
        last = frames[-1]
        d["location"] = f"{last.file}:{last.line}"
        d["frames"] = [f.as_dict() for f in frames]
        if dropped:
            d["frames_omitted"] = dropped
    extra = [ln for ln in c.e_lines[1:12] if ln.strip()]
    if extra and c.kind != "collection" and "\n" not in c.message:
        d["detail"] = "\n".join(extra)
    if with_output:
        if c.stdout:
            d["stdout"] = clip(c.stdout)
        if c.stderr:
            d["stderr"] = clip(c.stderr)
    return d


def build_result(
    report: Report,
    proc: ProcResult,
    interp: Interpreter | None,
    project: Path,
    command: list[str],
) -> dict[str, Any]:
    cases = report.cases
    summary = {o: 0 for o in OUTCOMES}
    tests = [c for c in cases if c.kind == "test"]
    for c in tests:
        summary[c.outcome] += 1
    summary["total"] = len(tests)
    coll = report.collection_errors()
    summary["collection_errors"] = len(coll)

    if report.problem == "timeout":
        outcome = "timeout"
    elif report.problem:
        outcome = report.problem
    elif not tests and not coll:
        outcome = "no_tests"
    elif summary["failed"] or summary["error"] or coll:
        outcome = "failed"
    else:
        outcome = "passed"

    res: dict[str, Any] = {
        "runner": report.runner,
        "project": str(project),
        "interpreter": f"{interp.display()} ({interp.source})" if interp else "node",
        "command": " ".join(command),
        "outcome": outcome,
        "summary": summary,
        "duration_s": round(proc.duration, 2),
        "exit_code": report.exit_code,
    }
    failing = report.failing()
    if failing:
        res["failures"] = [case_dict(c, project) for c in failing[:MAX_FAILURES]]
        if len(failing) > MAX_FAILURES:
            rest = [c.id for c in failing[MAX_FAILURES:]]
            res["failures_not_shown"] = {
                "count": len(rest),
                "ids": rest[:MAX_OTHER_IDS],
                "hint": "call explain_failure(test_id) for any of these, or rerun_failed",
            }
    if coll:
        res["collection_errors"] = [case_dict(c, project, False) for c in coll[:MAX_COLLECTION_ERRORS]]
        if len(coll) > MAX_COLLECTION_ERRORS:
            res["collection_errors_not_shown"] = len(coll) - MAX_COLLECTION_ERRORS
    timed = sorted((c for c in tests if c.duration > 0), key=lambda c: -c.duration)[:SLOWEST]
    if timed:
        res["slowest"] = [{"id": c.id, "seconds": round(c.duration, 3)} for c in timed]
    reasons: dict[str, int] = {}
    for c in tests:
        if c.outcome in ("skipped", "xfailed") and c.reason:
            reasons[c.reason] = reasons.get(c.reason, 0) + 1
    if reasons:
        top = sorted(reasons.items(), key=lambda kv: -kv[1])[:5]
        res["skip_reasons"] = {k[:120]: v for k, v in top}
    if report.problem:
        if report.hung_test:
            res["running_when_stopped"] = report.hung_test
        if report.finished_before_stop:
            res["finished_before_stop"] = report.finished_before_stop
        res["output_tail"] = clip(proc.tail or proc.head, 2500)
    notes = list(report.notes)
    if proc.leftover_killed:
        notes.append(
            "a process started by the tests was still running after they finished and "
            "was killed (it kept the output pipe open)"
        )
    if proc.truncated:
        notes.append(f"{proc.dropped_bytes} bytes of test output were dropped (output cap)")
    if notes:
        res["notes"] = notes
    return res
