"""Operator-controlled configuration (command-line flags, not tool arguments).

Anything that weakens the sandbox is a flag the person who installs the server
sets. An agent cannot widen it through a tool call.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from .errors import TestrunError

# Variables that a test process needs just to start and behave normally.
# Everything else (API keys, tokens, cloud credentials) is dropped by default.
BASE_ENV_ALLOW = frozenset(
    name.upper()
    for name in (
        "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC",
        "TEMP", "TMP", "TMPDIR", "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
        "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES",
        "PROGRAMFILES(X86)", "COMMONPROGRAMFILES", "NUMBER_OF_PROCESSORS",
        "PROCESSOR_ARCHITECTURE", "OS", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TERM",
    )
)

# Always set for the child, whatever the scrubbing mode.
FORCED_ENV = {
    "PYTHONUNBUFFERED": "1",
    "PYTHONIOENCODING": "utf-8",
    "PYTHONUTF8": "1",
    "NO_COLOR": "1",
    "FORCE_COLOR": "0",
}


@dataclass
class Config:
    sandbox: Path | None = None
    allow_env: frozenset[str] = frozenset()
    scrub_env: bool = True

    def resolve_project(self, path: str) -> Path:
        p = Path(path).expanduser()
        try:
            p = p.resolve(strict=True)
        except (FileNotFoundError, OSError) as e:
            raise TestrunError(
                f"path {path!r} does not exist (resolved against the server's working "
                f"directory {os.getcwd()!r}); pass an absolute project directory"
            ) from e
        if not p.is_dir():
            raise TestrunError(f"path {path!r} is not a directory; pass the project root")
        if self.sandbox is not None and not _inside(p, self.sandbox):
            raise TestrunError(
                f"path {str(p)!r} is outside the sandbox directory {str(self.sandbox)!r} "
                "(the server was started with --dir)"
            )
        return p

    def build_env(self) -> dict[str, str]:
        if self.scrub_env:
            allow = BASE_ENV_ALLOW | self.allow_env
            env = {k: v for k, v in os.environ.items() if k.upper() in allow}
        else:
            env = dict(os.environ)
        env.update(FORCED_ENV)
        return env


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


CONFIG = Config()


def set_config(cfg: Config) -> None:
    global CONFIG
    CONFIG = cfg


def parse_sandbox(value: str) -> Path:
    p = Path(value).expanduser()
    if not p.is_dir():
        print(f"testrun-mcp: --dir {value!r} is not a directory", file=sys.stderr)
        raise SystemExit(2)
    return p.resolve()
