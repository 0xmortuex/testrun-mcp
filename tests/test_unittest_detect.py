"""unittest support through the bundled helper, plus runner detection."""

from __future__ import annotations

import sys

import pytest
from _util import write_project

from testrun_mcp import service
from testrun_mcp.errors import TestrunError

UNITTEST_PROJECT = {
    "tests/__init__.py": "",
    "tests/test_u.py": """
        import time
        import unittest


        class T(unittest.TestCase):
            def test_pass(self):
                self.assertEqual(1, 1)

            def test_fail(self):
                print("out text")
                self.assertEqual(1, 2)

            def test_error(self):
                raise ValueError("kaboom")

            @unittest.skip("later")
            def test_skip(self):
                pass

            @unittest.expectedFailure
            def test_xfail(self):
                self.assertTrue(False)

            @unittest.expectedFailure
            def test_xpass(self):
                pass

            def test_sub(self):
                for i in range(2):
                    with self.subTest(i=i):
                        self.assertEqual(i, 0)

            def test_hang(self):
                if "hang" in open("mode.txt").read():
                    time.sleep(600)
    """,
    "tests/test_bad.py": "import not_here_at_all_xyz\n",
    "mode.txt": "normal\n",
}


def run(cfg, project, **kw):
    args = dict(selector=None, timeout=120, extra_args=None, python=None, runner=None)
    args.update(kw)
    return service.run_tests(cfg, str(project), **args)


def test_unittest_is_detected_and_run(cfg, tmp_path):
    p = write_project(tmp_path / "proj", UNITTEST_PROJECT)
    d = service.detect(cfg, str(p), None)
    assert d["primary"] == "unittest"
    r = run(cfg, p)
    assert r["runner"] == "unittest" and r["outcome"] == "failed"
    s = r["summary"]
    assert (s["passed"], s["skipped"], s["xfailed"], s["xpassed"]) == (2, 1, 1, 1)
    assert s["error"] == 1 and s["collection_errors"] == 1
    f = {x["id"]: x for x in r["failures"]}
    fail = f["tests.test_u.T.test_fail"]
    assert fail["message"].startswith("AssertionError: 1 != 2")
    assert fail["location"] == "tests/test_u.py:11"
    assert fail["stdout"] == "out text"
    assert fail["frames"][-1]["code"] == "self.assertEqual(1, 2)"
    assert f["tests.test_u.T.test_error"]["message"] == "ValueError: kaboom"
    assert f["tests.test_u.T.test_error"]["kind"] == "error"
    assert any(i.startswith("tests.test_u.T.test_sub") for i in f)
    ce = r["collection_errors"]
    assert [c["id"] for c in ce] == ["tests.test_bad"]
    assert "not_here_at_all_xyz" in ce[0]["message"]


def test_unittest_selector_rerun_and_flaky(cfg, tmp_path):
    p = write_project(tmp_path / "proj", UNITTEST_PROJECT)
    r = run(cfg, p, selector="tests.test_u.T.test_pass")
    assert r["outcome"] == "passed" and r["summary"]["total"] == 1
    r = run(cfg, p, selector="tests.test_u.T.test_fail")
    assert r["summary"]["failed"] == 1
    again = service.rerun_failed(cfg, str(p), 60, None)
    assert again["summary"]["failed"] == 1 and again["rerun_ids"] == 1
    fl = service.flaky_check(cfg, str(p), "tests.test_u.T.test_pass", 3, 60, None, None)
    assert fl["pattern"] == "PPP" and fl["runner"] == "unittest"
    ex = service.explain_failure(cfg, str(p), "tests.test_u.T.test_error", 60, None, None)
    assert ex["outcome"] == "error" and "ValueError: kaboom" in ex["traceback"]
    r = run(cfg, p, selector="tests.test_u.T", extra_args=["-k", "pass"])
    assert r["summary"]["total"] == 2  # test_pass and test_xpass match *pass*


def test_unittest_list_tests(cfg, tmp_path):
    p = write_project(tmp_path / "proj", UNITTEST_PROJECT)
    r = service.list_tests(cfg, str(p), None, None, 60, None, None)
    assert r["runner"] == "unittest"
    assert "tests.test_u.T.test_pass" in r["tests"] and r["count"] == 8
    assert r["collection_errors"] == ["tests.test_bad"]
    r = service.list_tests(cfg, str(p), None, "fail", 60, None, None)
    assert r["tests"] == ["tests.test_u.T.test_fail", "tests.test_u.T.test_xfail"]


def test_unittest_hang_is_named(cfg, tmp_path):
    p = write_project(tmp_path / "proj", UNITTEST_PROJECT)
    (p / "mode.txt").write_text("hang\n", encoding="utf-8")
    r = run(cfg, p, selector="tests.test_u.T.test_hang", timeout=15)
    assert r["outcome"] == "timeout"
    assert r["running_when_stopped"] == "tests.test_u.T.test_hang"


def test_detect_pytest_project(cfg, tmp_path):
    p = write_project(tmp_path / "proj", {
        "pyproject.toml": """
            [project]
            name = "x"

            [tool.pytest.ini_options]
            testpaths = ["tests"]
        """,
        "tests/conftest.py": "",
        "tests/test_a.py": "def test_a(): pass\n",
    })
    d = service.detect(cfg, str(p), None)
    assert d["primary"] == "pytest"
    r0 = d["runners"][0]
    assert r0["runner"] == "pytest" and r0["ready"] is True
    assert any("pyproject.toml" in e for e in r0["evidence"])
    assert any("conftest.py" in e for e in r0["evidence"])
    assert sys.executable in r0["how"] and "--junitxml" in r0["how"]
    assert d["python"]["usable"] and d["python"]["pytest"] and d["python"]["source"] == "server interpreter"
    assert d["test_dirs"] == ["tests"]
    assert d["python_test_files"] == 1


def test_detect_reports_missing_pytest(cfg, tmp_path, bare_venv):
    from conftest import venv_python

    p = write_project(tmp_path / "proj", {"tests/test_a.py": "import pytest\n\ndef test_a(): pass\n"})
    d = service.detect(cfg, str(p), str(venv_python(bare_venv)))
    r0 = d["runners"][0]
    assert r0["runner"] == "pytest" and r0["ready"] is False
    assert "not installed" in r0["problem"]
    assert d["python"]["pytest"] is None and d["python"]["usable"] is True


def test_detect_js_projects(cfg, tmp_path):
    jest = write_project(tmp_path / "j", {
        "package.json": """
            {"name": "x", "scripts": {"test": "jest"}, "devDependencies": {"jest": "^29.0.0"}}
        """,
        "jest.config.js": "module.exports = {};\n",
        "__tests__/a.test.js": "test('a', () => {});\n",
    })
    d = service.detect(cfg, str(jest), None)
    assert d["primary"] == "jest"
    r0 = d["runners"][0]
    assert r0["ready"] is False and "npm ci" in r0["problem"]
    assert any("jest.config.js" in e for e in r0["evidence"])
    assert d["npm_test_script"] == "jest" and "__tests__" in d["test_dirs"]
    vt = write_project(tmp_path / "v", {
        "package.json": '{"devDependencies": {"vitest": "^2.0.0"}}\n',
        "vitest.config.ts": "export default {}\n",
    })
    assert service.detect(cfg, str(vt), None)["primary"] == "vitest"


def test_detect_empty_project_and_bad_paths(cfg, tmp_path):
    (tmp_path / "empty").mkdir()
    d = service.detect(cfg, str(tmp_path / "empty"), None)
    assert d["primary"] is None and d["runners"] == [] and "no pytest" in d["note"]
    with pytest.raises(TestrunError, match="no test runner detected"):
        run(cfg, tmp_path / "empty")
    with pytest.raises(TestrunError, match="does not exist"):
        service.detect(cfg, str(tmp_path / "missing"), None)


def test_detect_ignores_vendored_and_venv_dirs(cfg, tmp_path):
    p = write_project(tmp_path / "proj", {
        "node_modules/pkg/test_fake.py": "import pytest\n",
        ".venv/lib/test_other.py": "import pytest\n",
        "app.py": "x = 1\n",
    })
    assert service.detect(cfg, str(p), None)["primary"] is None


def test_runner_override(cfg, tmp_path):
    p = write_project(tmp_path / "proj", UNITTEST_PROJECT)
    r = run(cfg, p, runner="pytest", selector="tests/test_u.py::T::test_pass")
    assert r["runner"] == "pytest" and r["outcome"] == "passed"
    with pytest.raises(TestrunError, match="runner must be"):
        run(cfg, p, runner="mocha")
