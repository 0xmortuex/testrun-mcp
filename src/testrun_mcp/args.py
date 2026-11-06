"""Validation of everything an agent can put on the child's command line.

The command is always an argv list (no shell), so this is not about quoting.
It is about *meaning*: `-p some.module`, `-c other.ini`, `--rootdir`, `--basetemp`,
`--junitxml`, `--config`, `--require`, `--setupFiles` and friends all change what
code runs or where results go. Only the flags below pass; anything else is
rejected with the list of what is allowed.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from .errors import TestrunError

# kind: "flag" (no value), "int", "str", "path", or "a|b|c" (choice)
_PYTEST: dict[str, str] = {
    "-x": "flag", "--exitfirst": "flag",
    "--maxfail": "int",
    "-k": "str", "-m": "str",
    "--deselect": "str", "--ignore": "path",
    "--runxfail": "flag", "--strict-markers": "flag", "--strict-config": "flag",
    "-l": "flag", "--showlocals": "flag",
    "--durations": "int",
    "-W": "str",
    "--import-mode": "prepend|append|importlib",
    "--disable-warnings": "flag",
}
_UNITTEST: dict[str, str] = {"-f": "flag", "--failfast": "flag", "-k": "str"}
_JEST: dict[str, str] = {
    "--bail": "flag", "-b": "flag", "--runInBand": "flag", "-i": "flag",
    "-t": "str", "--testNamePattern": "str", "--maxWorkers": "int",
}
_VITEST: dict[str, str] = {
    "--bail": "int", "-t": "str", "--testNamePattern": "str",
    "--no-file-parallelism": "flag", "--retry": "int",
    "--pool": "threads|forks|vmThreads|vmForks",
}
ALLOWED: dict[str, dict[str, str]] = {
    "pytest": _PYTEST, "unittest": _UNITTEST, "jest": _JEST, "vitest": _VITEST,
}


def parse_extra_args(runner: str, extra_args: list[str] | None) -> list[str]:
    """Validate extra_args against the allow-list and return argv tokens."""
    if not extra_args:
        return []
    table = ALLOWED[runner]
    out: list[str] = []
    items = list(extra_args)
    i = 0
    while i < len(items):
        raw = items[i]
        i += 1
        flag, eq, inline = raw.partition("=")
        kind = table.get(flag)
        if kind is None:
            raise TestrunError(
                f"extra_args: {raw!r} is not allowed for {runner}. Allowed flags: "
                + ", ".join(sorted(table))
                + ". (Arbitrary options are refused because flags like -p, -c, --rootdir "
                "or --require can load code or redirect output.)"
            )
        if kind == "flag":
            if eq:
                raise TestrunError(f"extra_args: {flag} does not take a value")
            out.append(flag)
            continue
        if eq:
            value = inline
        elif i < len(items):
            value = items[i]
            i += 1
        else:
            raise TestrunError(f"extra_args: {flag} needs a value")
        out.extend(_value(runner, flag, kind, value))
    return out


def _value(runner: str, flag: str, kind: str, value: str) -> list[str]:
    if "|" in kind:
        if value not in kind.split("|"):
            raise TestrunError(f"extra_args: {flag} must be one of {kind.split('|')}")
        return [f"{flag}={value}"]
    if kind == "int":
        if not re.fullmatch(r"\d{1,6}", value):
            raise TestrunError(f"extra_args: {flag} needs a non-negative integer, got {value!r}")
        return [f"{flag}={value}"]
    if kind in ("str", "path"):
        if value.startswith("-"):
            raise TestrunError(f"extra_args: the value of {flag} may not start with '-'")
        if "\x00" in value:
            raise TestrunError(f"extra_args: the value of {flag} contains a NUL byte")
        # Attached form so the value can never be parsed as another option.
        return [f"{flag}={value}"] if flag.startswith("--") else [flag, value]
    raise AssertionError(kind)


def check_selector(runner: str, project: Path, selector: str | None) -> str | None:
    """A selector narrows what runs. It must not start with '-' (that would be
    an option) and, for file-based selectors, must stay inside the project."""
    if selector is None or selector.strip() == "":
        return None
    if selector.startswith("-"):
        raise TestrunError(
            f"selector {selector!r} starts with '-': a selector is a path, node id, "
            "dotted name or name pattern, not an option (use extra_args for options)"
        )
    if "\x00" in selector:
        raise TestrunError("selector contains a NUL byte")
    if runner == "pytest":
        check_inside(project, selector.split("::", 1)[0], "selector")
    return selector


def check_inside(project: Path, rel: str, what: str) -> None:
    if rel == "":
        return
    candidate = Path(rel)
    full = candidate if candidate.is_absolute() else project / candidate
    resolved = Path(os.path.normpath(full))
    try:
        resolved.relative_to(project)
    except ValueError as e:
        raise TestrunError(
            f"{what} {rel!r} points outside the project directory {str(project)!r}"
        ) from e
