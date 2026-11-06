"""Choose the Python interpreter that runs the target project's tests.

The server's own interpreter almost never has the project's dependencies, so
the order is: explicit `python=` argument, then a virtualenv inside the project
(.venv, venv, env, .env), then the server's own interpreter. Every result says
which interpreter was used.
"""

from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from .errors import TestrunError

VENV_DIRS = (".venv", "venv", "env", ".env")


@dataclass
class Interpreter:
    argv: list[str]  # prefix: [python] or [uv, run, --frozen, python]
    source: str  # "argument", "project venv", "server interpreter", "uv run"

    def display(self) -> str:
        return " ".join(self.argv)


def venv_python(root: Path) -> Path | None:
    for d in VENV_DIRS:
        for rel in (("Scripts", "python.exe"), ("bin", "python")):
            cand = root.joinpath(d, *rel)
            if cand.is_file():
                return cand
    return None


def find_interpreter(project: Path, python: str | None) -> Interpreter:
    if python is not None and python.strip() != "":
        if python.strip().lower() == "uv":
            uv = shutil.which("uv")
            if uv is None:
                raise TestrunError(
                    "python='uv' was requested but `uv` is not on PATH. Install uv or pass "
                    "the path to a python executable."
                )
            return Interpreter([uv, "run", "--frozen", "python"], "uv run")
        p = Path(python).expanduser()
        if not p.is_file():
            found = shutil.which(python)
            if found is None:
                raise TestrunError(
                    f"python={python!r} is not an existing file or a command on PATH. Pass the "
                    "full path to the interpreter, e.g. C:/proj/.venv/Scripts/python.exe "
                    "or /proj/.venv/bin/python."
                )
            p = Path(found)
        if not p.name.lower().startswith(("python", "pypy")):
            raise TestrunError(
                f"python={python!r}: the executable must be named python* or pypy* "
                "(this option selects an interpreter, it does not run arbitrary programs)"
            )
        return Interpreter([str(p)], "argument")
    venv = venv_python(project)
    if venv is not None:
        return Interpreter([str(venv)], "project venv")
    return Interpreter([sys.executable], "server interpreter")


def missing_module_hint(interp: Interpreter, module: str) -> str:
    return (
        f"`{module}` is not installed for the interpreter that was used "
        f"({interp.display()}, chosen from {interp.source}). Install it there "
        f"(e.g. `{interp.argv[0]} -m pip install {module}` or `uv pip install {module}`), "
        "or pass python= with the path of the project's virtualenv interpreter "
        "(python='uv' runs it through `uv run`)."
    )


