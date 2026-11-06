"""Work out which test runner a project uses, without running any of its code."""

from __future__ import annotations

import configparser
import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Config
from .errors import TestrunError
from .procs import run_process
from .pyenv import Interpreter

SKIP_DIRS = {
    ".git", ".hg", ".venv", "venv", "env", ".env", "node_modules", "__pycache__",
    ".tox", ".nox", "build", "dist", ".mypy_cache", ".ruff_cache", ".pytest_cache",
    "site-packages",
}
TEST_DIR_NAMES = {"tests", "test", "testing", "__tests__", "spec"}
CODE_SUFFIXES = (".py", ".js", ".ts", ".mjs", ".cjs", ".jsx", ".tsx")
MAX_SCAN_FILES = 2000
JS_CONFIG_EXTS = ("js", "ts", "mjs", "cjs", "mts", "cts", "json")


@dataclass
class Candidate:
    runner: str
    evidence: list[str] = field(default_factory=list)
    score: int = 0


def _walk(project: Path, max_depth: int = 3) -> list[Path]:
    out: list[Path] = []
    base_depth = len(project.parts)
    for root, dirs, files in os.walk(project):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.endswith(".egg-info")]
        if len(Path(root).parts) - base_depth >= max_depth:
            dirs[:] = []
        for f in files:
            out.append(Path(root) / f)
            if len(out) >= MAX_SCAN_FILES:
                return out
    return out


def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _slash(p: Path) -> str:
    return str(p).replace("\\", "/")


def _pytest_config(project: Path) -> list[str]:
    found: list[str] = []
    if (project / "pytest.ini").is_file():
        found.append("pytest.ini")
    tox = project / "tox.ini"
    if tox.is_file():
        cp = configparser.ConfigParser(interpolation=None, strict=False)
        try:
            cp.read_string(_read(tox))
            if cp.has_section("pytest"):
                found.append("tox.ini [pytest]")
        except configparser.Error:
            pass
    setup = project / "setup.cfg"
    if setup.is_file() and "[tool:pytest]" in _read(setup):
        found.append("setup.cfg [tool:pytest]")
    pyproject = project / "pyproject.toml"
    if pyproject.is_file() and "[tool.pytest" in _read(pyproject):
        found.append("pyproject.toml [tool.pytest.ini_options]")
    return found


def _package_json(project: Path) -> dict[str, Any]:
    p = project / "package.json"
    if not p.is_file():
        return {}
    try:
        data = json.loads(_read(p))
    except json.JSONDecodeError as e:
        raise TestrunError(f"{p} is not valid JSON: {e}") from e
    return data if isinstance(data, dict) else {}


def scan(project: Path) -> tuple[list[Candidate], dict[str, Any]]:
    rel = [f.relative_to(project) for f in _walk(project)]
    info: dict[str, Any] = {}
    cands: dict[str, Candidate] = {}

    def add(name: str, score: int, evidence: str) -> None:
        c = cands.setdefault(name, Candidate(name))
        c.score += score
        c.evidence.append(evidence)

    py_tests = [r for r in rel if r.suffix == ".py"
                and (r.name.startswith("test_") or r.name.endswith("_test.py"))]
    conftests = [r for r in rel if r.name == "conftest.py"]
    test_dirs = sorted({
        _slash(r.parent) for r in rel
        if any(part in TEST_DIR_NAMES for part in r.parts[:-1]) and r.suffix in CODE_SUFFIXES
    })
    info["test_dirs"] = test_dirs[:20]
    info["python_test_files"] = len(py_tests)

    for x in _pytest_config(project):
        add("pytest", 5, f"config: {x}")
    if conftests:
        add("pytest", 4, f"conftest.py: {_slash(conftests[0])}")
    deps = " ".join(
        _read(project / n) for n in
        ("pyproject.toml", "requirements.txt", "requirements-dev.txt", "setup.cfg", "tox.ini")
    )
    if "pytest" in deps.lower():
        add("pytest", 3, "pytest is listed in dependency files")
    pytest_style = [r for r in py_tests[:60] if "import pytest" in _read(project / r)]
    if pytest_style:
        add("pytest", 4, f"imports pytest: {_slash(pytest_style[0])}")
    unittest_files = [r for r in py_tests[:60] if "unittest" in _read(project / r)]
    if unittest_files:
        add("unittest", 2, f"uses unittest: {_slash(unittest_files[0])}")
    if py_tests and not cands:
        add("pytest", 1, f"{len(py_tests)} test_*.py files (pytest default)")

    pkg = _package_json(project)
    if pkg:
        all_deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
        scripts = pkg.get("scripts", {}) if isinstance(pkg.get("scripts"), dict) else {}
        info["npm_test_script"] = scripts.get("test")
        names = {p.name for p in rel}
        for name in ("vitest", "jest"):
            hits: list[str] = []
            if name in all_deps:
                hits.append(f"package.json depends on {name} ({all_deps[name]})")
            hits += [f"config: {name}.config.{e}" for e in JS_CONFIG_EXTS
                     if f"{name}.config.{e}" in names]
            if name in str(scripts.get("test", "")):
                hits.append(f"npm test script runs {name}")
            if name == "jest" and "jest" in pkg:
                hits.append("package.json has a 'jest' config block")
            for h in hits:
                add(name, 2, h)
            if hits and (project / "node_modules" / name).is_dir():
                cands[name].evidence.append(f"node_modules/{name} is installed")
            elif hits:
                cands[name].evidence.append(f"node_modules/{name} is NOT installed (run npm ci)")
    return sorted(cands.values(), key=lambda c: -c.score), info


def probe_python(cfg: Config, project: Path, interp: Interpreter) -> dict[str, Any]:
    """Ask the chosen interpreter what it has installed (one short process)."""
    code = (
        "import importlib.util as u,sys\n"
        "def v(n):\n"
        "    if u.find_spec(n) is None: return None\n"
        "    try:\n"
        "        import importlib.metadata as m; return m.version(n)\n"
        "    except Exception: return 'installed'\n"
        "print(sys.version.split()[0], v('pytest'), v('coverage'))\n"
    )
    base = {"interpreter": interp.display(), "source": interp.source}
    try:
        r = run_process([*interp.argv, "-c", code], str(project), cfg.build_env(), 30)
    except OSError as e:
        return {**base, "usable": False, "problem": str(e)}
    parts = (r.text().strip().splitlines() or [""])[-1].split()
    if r.timed_out or r.returncode != 0 or len(parts) != 3:
        return {**base, "usable": False,
                "problem": r.text().strip()[-300:] or "interpreter did not run"}

    def opt(s: str) -> str | None:
        return None if s == "None" else s

    return {**base, "usable": True, "python": parts[0], "pytest": opt(parts[1]),
            "coverage": opt(parts[2])}


def how_it_runs(runner: str, interp: Interpreter | None) -> str:
    if runner == "pytest" and interp:
        return (f"{interp.display()} -m pytest --junitxml=<tmp> -p no:cacheprovider --tb=short "
                "-v --continue-on-collection-errors [selector]")
    if runner == "unittest" and interp:
        return (f"{interp.display()} <bundled helper> [dotted.names]  "
                "(discovery from the project root)")
    if runner in ("jest", "vitest"):
        entry = ("node_modules/jest/bin/jest.js --json" if runner == "jest"
                 else "node_modules/vitest/vitest.mjs run --reporter=json")
        return f"{shutil.which('node') or 'node'} {entry} --outputFile=<tmp> [file patterns]"
    return ""


def choose_runner(project: Path, runner: str | None) -> str:
    if runner:
        if runner not in ("pytest", "unittest", "jest", "vitest"):
            raise TestrunError("runner must be one of: pytest, unittest, jest, vitest")
        return runner
    cands, _ = scan(project)
    if not cands:
        raise TestrunError(
            f"no test runner detected in {project}: no pytest/unittest test files and no "
            "jest/vitest in package.json. Run detect_runner for details, or pass runner= explicitly."
        )
    return cands[0].runner
