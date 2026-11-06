"""Process runner: timeout, tree kill, output caps, stdin, no shell."""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest
from _util import wait_dead, wait_file

from testrun_mcp import procs
from testrun_mcp.config import FORCED_ENV, Config
from testrun_mcp.procs import run_process


SLEEPER_DEF = """
SLEEPER = "import os, sys, time; open(sys.argv[1], 'w').write(str(os.getpid())); time.sleep(600)"
"""


def run_py(code: str, tmp_path: Path, timeout: float = 30, args: list[str] | None = None):
    import os

    argv = [sys.executable, "-c", textwrap.dedent(code), *(args or [])]
    return run_process(argv, str(tmp_path), dict(os.environ), timeout)


def test_captures_output_and_exit_code(tmp_path):
    r = run_py("import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)", tmp_path)
    assert r.returncode == 3 and not r.timed_out
    assert "out" in r.text() and "err" in r.text()
    assert not r.truncated


def test_hard_timeout_kills_process_tree(tmp_path):
    pidfile = tmp_path / "grandchild.pid"
    code = """
        import subprocess, sys, time
        child = subprocess.Popen([sys.executable, "-c", SLEEPER, sys.argv[1]])
        time.sleep(600)
    """
    r = run_py(SLEEPER_DEF + textwrap.dedent(code), tmp_path, timeout=10, args=[str(pidfile)])
    assert r.timed_out and r.returncode is None
    assert r.duration < 20
    pid = int(wait_file(pidfile))
    assert wait_dead(pid), "the grandchild survived the timeout kill"


def test_descendant_that_outlives_the_run_is_killed(tmp_path):
    pidfile = tmp_path / "orphan.pid"
    code = """
        import subprocess, sys
        subprocess.Popen([sys.executable, "-c", SLEEPER, sys.argv[1]])
        print("parent exits now")
    """
    r = run_py(SLEEPER_DEF + textwrap.dedent(code), tmp_path, timeout=30, args=[str(pidfile)])
    assert r.returncode == 0 and not r.timed_out
    pid = int(wait_file(pidfile))
    assert wait_dead(pid), "an orphaned descendant was left running"
    # It inherited our output pipe, so the runner had to cut it loose.
    assert r.leftover_killed
    assert r.duration < 20


def test_huge_output_is_capped(tmp_path):
    r = run_py(
        "import sys\n"
        "for i in range(60000): sys.stdout.write('line %06d ' % i + 'x' * 90 + chr(10))\n"
        "print('THE END')\n",
        tmp_path,
    )
    assert r.returncode == 0
    assert r.total_bytes > 5_000_000
    assert r.truncated and r.dropped_bytes > 5_000_000 - procs.HEAD_BYTES - procs.TAIL_BYTES - 10
    assert len(r.head.encode()) <= procs.HEAD_BYTES and len(r.tail.encode()) <= procs.TAIL_BYTES
    text = r.text()
    assert "line 000000" in text and "THE END" in text and "bytes of output dropped" in text
    assert len(text) < 200_000


def test_stdin_is_closed(tmp_path):
    r = run_py(
        "import sys\ndata = sys.stdin.read()\nprint('stdin-eof', repr(data))", tmp_path, timeout=20
    )
    assert not r.timed_out and "stdin-eof ''" in r.text()


def test_no_shell_interpretation(tmp_path):
    import os

    r = run_process(
        [sys.executable, "-c", "import sys; print(sys.argv[1])", "a & echo INJECTED; $(whoami)"],
        str(tmp_path), dict(os.environ), 20,
    )
    assert r.text().strip() == "a & echo INJECTED; $(whoami)"


def test_unstartable_program_raises_oserror(tmp_path):
    import os

    with pytest.raises(OSError):
        run_process(["definitely-not-a-program-xyz"], str(tmp_path), dict(os.environ), 5)


def test_env_scrubbing(monkeypatch):
    monkeypatch.setenv("TESTRUN_SECRET_TOKEN", "hunter2")
    monkeypatch.setenv("OTHER_KEY", "v")
    scrubbed = Config().build_env()
    assert "TESTRUN_SECRET_TOKEN" not in scrubbed and "OTHER_KEY" not in scrubbed
    assert "PATH" in scrubbed
    for k, v in FORCED_ENV.items():
        assert scrubbed[k] == v
    allowed = Config(allow_env=frozenset({"TESTRUN_SECRET_TOKEN"})).build_env()
    assert allowed["TESTRUN_SECRET_TOKEN"] == "hunter2" and "OTHER_KEY" not in allowed
    inherit = Config(scrub_env=False).build_env()
    assert inherit["TESTRUN_SECRET_TOKEN"] == "hunter2" and inherit["OTHER_KEY"] == "v"
