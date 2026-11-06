"""jest/vitest support, driven through a real `node` process.

test_node.py uses a tiny stand-in for the jest CLI (tests/fixtures/fake_node/jest.js)
so the default run needs only node, not npm or network. test_real_js.py runs the
real jest and vitest when they have been installed with `npm ci`.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from _util import write_project

from testrun_mcp import service
from testrun_mcp.errors import TestrunError

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")

FAKE = Path(__file__).parent / "fixtures" / "fake_node" / "jest.js"


@pytest.fixture
def jest_project(tmp_path):
    p = write_project(tmp_path / "proj", {
        "package.json": '{"name": "x", "devDependencies": {"jest": "29.0.0"}}\n',
        # The stand-in jest reports on these files; they must exist for rerun_failed.
        "src/math.test.js": "// stand-in\n",
        "src/other.test.js": "// stand-in\n",
        "src/broken.test.js": "// stand-in\n",
    })
    dest = p / "node_modules" / "jest" / "bin"
    dest.mkdir(parents=True)
    shutil.copy(FAKE, dest / "jest.js")
    return p


def run(cfg, project, **kw):
    args = dict(selector=None, timeout=60, extra_args=None, python=None, runner="jest")
    args.update(kw)
    return service.run_tests(cfg, str(project), **args)


def test_jest_results_are_parsed(cfg, jest_project):
    r = run(cfg, jest_project)
    assert r["runner"] == "jest" and r["interpreter"] == "node" and r["outcome"] == "failed"
    s = r["summary"]
    assert (s["passed"], s["failed"], s["skipped"], s["total"], s["collection_errors"]) == (3, 1, 1, 5, 1)
    assert "--outputFile=<tmp>" in r["command"]
    f = r["failures"][0]
    assert f["id"] == "src/math.test.js::math fails"
    assert f["message"] == "Error: expect(received).toBe(expected) // Object.is equality"
    assert f["location"] == "src/math.test.js:7"
    assert [x["file"] for x in f["frames"]] == ["src/math.test.js"]
    assert f["detail"] == "Expected: 2\nReceived: 3"
    ce = r["collection_errors"][0]
    assert ce["id"] == "src/broken.test.js"
    assert ce["message"] == "Cannot find module './nope' from 'src/broken.test.js'"
    assert ce["location"] == "src/broken.test.js:1"


def test_jest_selector_and_name_filter(cfg, jest_project):
    r = run(cfg, jest_project, selector="other")
    assert r["summary"]["total"] == 2 and r["summary"]["collection_errors"] == 0
    r = run(cfg, jest_project, selector="math", extra_args=["-t", "adds"])
    assert r["outcome"] == "passed" and r["summary"]["total"] == 1


def test_jest_rerun_failed_uses_exact_ids(cfg, jest_project):
    run(cfg, jest_project)
    again = service.rerun_failed(cfg, str(jest_project), 60, None)
    assert again["rerun_ids"] == 2  # the failing test and the suite that failed to run
    # The broken suite has no test names, so whole files are rerun (no -t filter).
    assert again["summary"]["failed"] == 1 and again["summary"]["collection_errors"] == 1
    assert [f["id"] for f in again["failures"]] == ["src/math.test.js::math fails"]
    run(cfg, jest_project, selector="math")
    only = service.rerun_failed(cfg, str(jest_project), 60, None)
    assert only["rerun_ids"] == 1 and only["summary"]["total"] == 1


def test_jest_flaky_check_and_explain(cfg, jest_project):
    r = service.flaky_check(
        cfg, str(jest_project), "src/other.test.js::other flaky", 4, 60, None, "jest")
    assert r["pattern"] == "PFPF" and r["verdict"] == "flaky"
    assert r["first_failure"]["location"] == "src/other.test.js:7"
    ok = service.flaky_check(
        cfg, str(jest_project), "src/other.test.js::other works", 2, 60, None, "jest")
    assert ok["pattern"] == "PP"
    ex = service.explain_failure(
        cfg, str(jest_project), "src/math.test.js::math fails", 60, None, "jest")
    assert ex["outcome"] == "failed" and "Expected: 2" in ex["traceback"]
    assert "locals_note" not in ex


def test_jest_timeout_kills_node(cfg, jest_project, monkeypatch):
    from testrun_mcp.config import Config

    hang = Config(scrub_env=False)
    monkeypatch.setenv("FAKE_JEST_HANG", "1")
    r = run(hang, jest_project, timeout=3)
    assert r["outcome"] == "timeout"


def test_jest_dangerous_args_refused(cfg, jest_project):
    with pytest.raises(TestrunError, match="not allowed"):
        run(cfg, jest_project, extra_args=["--setupFiles", "evil.js"])
    with pytest.raises(TestrunError, match="starts with '-'"):
        run(cfg, jest_project, selector="--config")


def test_jest_not_installed_is_explained(cfg, tmp_path):
    p = write_project(tmp_path / "proj", {"package.json": '{"devDependencies": {"jest": "29"}}\n'})
    with pytest.raises(TestrunError, match="npm ci"):
        run(cfg, p)


def test_node_missing_is_explained(cfg, jest_project, monkeypatch):
    monkeypatch.setenv("PATH", str(jest_project))
    with pytest.raises(TestrunError, match="node is not on PATH"):
        run(cfg, jest_project)


def test_jest_and_python_projects_coexist(cfg, jest_project):
    write_project(jest_project, {"tests/test_a.py": "def test_a(): pass\n"})
    d = service.detect(cfg, str(jest_project), None)
    assert {r["runner"] for r in d["runners"]} == {"jest", "pytest"}
