from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from testrun_mcp.config import Config


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def cfg() -> Config:
    return Config()


@pytest.fixture(scope="session")
def bare_venv(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A virtualenv with no packages at all: pytest and coverage are missing."""
    d = tmp_path_factory.mktemp("barevenv") / "v"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(d)], check=True)
    return d


def venv_python(venv: Path) -> Path:
    for rel in ("Scripts/python.exe", "bin/python"):
        if (venv / rel).exists():
            return venv / rel
    raise AssertionError("venv has no python")
