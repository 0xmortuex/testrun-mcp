"""run_tests / rerun_failed / list_tests against real pytest in real temp projects."""

from __future__ import annotations

import json
import os
import sys

import pytest
from _util import wait_dead, wait_file, write_project

from testrun_mcp import service
from testrun_mcp.errors import TestrunError

MIXED = {
    "src/m.py": """
        def f(x):
            return 1 / x
    """,
    "tests/test_a.py": """
        import sys
        import pytest
        from src.m import f


        class TestK:
            def test_fail(self):
                print("hello out")
                assert f(1) == 3

            def test_zero(self):
                f(0)


        @pytest.mark.parametrize("v", ["你好", "a b"])
        def test_p(v):
            assert v == "a b"


        @pytest.mark.xfail(reason="known bug")
        def test_xf():
            assert 0


        @pytest.mark.xfail(reason="fixed already")
        def test_xp():
            assert 1


        @pytest.mark.skip(reason="not today")
        def test_sk():
            pass


        @pytest.fixture
        def bad():
            raise RuntimeError("fixture exploded")


        def test_setup(bad):
            pass


        def test_exit():
            sys.exit(3)


        def test_ok():
            assert True
    """,
}


def run(cfg, project, **kw):
    args = dict(selector=None, timeout=120, extra_args=None, python=None, runner=None)
    args.update(kw)
    return service.run_tests(cfg, str(project), **args)


def by_id(items):
    return {i["id"]: i for i in items}


def test_all_passing(cfg, tmp_path):
    p = write_project(tmp_path / "proj", {
        "tests/test_ok.py": """
            def test_one(): assert 1 + 1 == 2
            def test_two(): assert "a".upper() == "A"
        """,
    })
    r = run(cfg, p)
    assert r["outcome"] == "passed" and r["runner"] == "pytest"
    assert r["summary"] == {
        "passed": 2, "failed": 0, "error": 0, "skipped": 0, "xfailed": 0, "xpassed": 0,
        "total": 2, "collection_errors": 0,
    }
    assert "failures" not in r and "collection_errors" not in r
    assert r["exit_code"] == 0
    assert sys.executable in r["interpreter"] and "server interpreter" in r["interpreter"]
    assert len(json.dumps(r)) < 3000


def test_mixed_outcomes_exact(cfg, tmp_path):
    p = write_project(tmp_path / "proj", MIXED)
    r = run(cfg, p)
    assert r["outcome"] == "failed"
    assert r["summary"] == {
        "passed": 2, "failed": 4, "error": 1, "skipped": 1, "xfailed": 1, "xpassed": 1,
        "total": 10, "collection_errors": 0,
    }
    f = by_id(r["failures"])
    assert set(f) == {
        "tests/test_a.py::TestK::test_fail",
        "tests/test_a.py::TestK::test_zero",
        "tests/test_a.py::test_p[\\u4f60\\u597d]",
        "tests/test_a.py::test_setup",
        "tests/test_a.py::test_exit",
    }
    fail = f["tests/test_a.py::TestK::test_fail"]
    assert fail["kind"] == "failure"
    assert fail["message"].startswith("assert 1.0 == 3")
    assert fail["location"] == "tests/test_a.py:9"
    assert fail["stdout"] == "hello out"
    assert fail["frames"][0]["code"] == "assert f(1) == 3"
    zero = f["tests/test_a.py::TestK::test_zero"]
    assert zero["message"] == "ZeroDivisionError: division by zero"
    assert zero["location"] == "src/m.py:2"  # innermost project frame
    assert [(x["file"], x["in"]) for x in zero["frames"]] == [
        ("tests/test_a.py", "test_zero"), ("src/m.py", "f"),
    ]
    assert f["tests/test_a.py::test_setup"]["kind"] == "error"
    assert "fixture exploded" in f["tests/test_a.py::test_setup"]["message"]
    assert f["tests/test_a.py::test_exit"]["message"] == "SystemExit: 3"
    assert r["skip_reasons"] == {"not today": 1, "known bug": 1}
    assert "slowest" in r and len(r["slowest"]) <= 5
    assert "next" in r


def test_unicode_ids_round_trip_through_rerun(cfg, tmp_path):
    p = write_project(tmp_path / "proj", {
        "tests/test_ünï.py": """
            import pytest


            def test_日本語():
                assert False, "über broken"


            @pytest.mark.parametrize("v", ["你好", "ok"])
            def test_p(v):
                assert v == "ok"
        """,
    })
    r = run(cfg, p)
    ids = {x["id"] for x in r["failures"]}
    assert "tests/test_ünï.py::test_日本語" in ids
    assert any(i.startswith("tests/test_ünï.py::test_p[") for i in ids)
    assert "über broken" in by_id(r["failures"])[
        "tests/test_ünï.py::test_日本語"]["message"]
    again = service.rerun_failed(cfg, str(p), 60, None)
    assert again["summary"]["total"] == 2 and again["summary"]["failed"] == 2
    assert {x["id"] for x in again["failures"]} == ids


def test_project_path_with_spaces(cfg, tmp_path):
    p = write_project(tmp_path / "my project dir", {
        "tests/test_s.py": """
            def test_a(): assert True
            def test_b(): assert False
        """,
    })
    r = run(cfg, p)
    assert r["summary"]["passed"] == 1 and r["summary"]["failed"] == 1
    assert r["failures"][0]["id"] == "tests/test_s.py::test_b"


def test_collection_error_is_separate_from_failures(cfg, tmp_path):
    p = write_project(tmp_path / "proj", {
        "tests/test_good.py": """
            def test_pass(): assert True
            def test_fail(): assert 1 == 2
        """,
        "tests/test_bad.py": """
            import module_that_does_not_exist_xyz

            def test_never(): pass
        """,
    })
    r = run(cfg, p)
    assert r["outcome"] == "failed"
    assert r["summary"]["collection_errors"] == 1
    assert r["summary"]["passed"] == 1 and r["summary"]["failed"] == 1
    assert [x["id"] for x in r["failures"]] == ["tests/test_good.py::test_fail"]
    ce = r["collection_errors"]
    assert len(ce) == 1 and ce[0]["id"] == "tests/test_bad.py" and ce[0]["kind"] == "collection_error"
    assert "module_that_does_not_exist_xyz" in ce[0]["message"]
    assert ce[0]["location"] == "tests/test_bad.py:1"


def test_module_level_sys_exit_is_an_internal_error(cfg, tmp_path):
    # pytest itself dies with INTERNALERROR when a test module calls sys.exit() on import.
    p = write_project(tmp_path / "proj", {
        "tests/test_exit.py": """
            import sys
            sys.exit(2)
        """,
        "tests/test_fine.py": "def test_fine(): pass\n",
    })
    r = run(cfg, p)
    assert r["outcome"] == "internal_error" and r["exit_code"] == 3
    assert "SystemExit: 2" in r["output_tail"]
    assert any("INTERNALERROR" in n for n in r["notes"])


def test_conftest_import_error_is_a_startup_error(cfg, tmp_path):
    p = write_project(tmp_path / "proj", {
        "conftest.py": "import nothing_here_either_xyz\n",
        "tests/test_a.py": "def test_a(): pass\n",
    })
    r = run(cfg, p)
    assert r["outcome"] == "startup_error"
    assert r["exit_code"] == 4
    assert "nothing_here_either_xyz" in r["output_tail"]
    assert r["summary"]["total"] == 0


def test_no_tests_collected(cfg, tmp_path):
    p = write_project(tmp_path / "proj", {"tests/test_a.py": "def helper(): pass\n"})
    r = run(cfg, p)
    assert r["outcome"] == "no_tests" and r["exit_code"] == 5
    assert any("collected no tests" in n for n in r["notes"])
    r2 = run(cfg, write_project(tmp_path / "p2", {"tests/test_a.py": "def test_a(): pass\n"}),
             extra_args=["-k", "does_not_match"])
    assert r2["outcome"] == "no_tests"


def test_hanging_test_times_out_and_is_named(cfg, tmp_path):
    p = write_project(tmp_path / "proj", {
        "tests/test_h.py": """
            import time


            def test_before():
                assert True


            def test_hang():
                time.sleep(600)


            def test_after():
                assert True
        """,
    })
    r = run(cfg, p, timeout=15)
    assert r["outcome"] == "timeout"
    assert r["exit_code"] is None
    assert r["running_when_stopped"] == "tests/test_h.py::test_hang"
    assert r["finished_before_stop"] == {"passed": 1}
    assert r["duration_s"] < 30


def test_hanging_test_with_child_process_leaves_nothing_behind(cfg, tmp_path):
    pidfile = tmp_path / "child.pid"
    p = write_project(tmp_path / "proj", {
        "tests/test_h.py": f"""
            import subprocess, sys, time


            def test_spawns_and_hangs():
                subprocess.Popen([sys.executable, "-c",
                    "import os, time, sys; open(sys.argv[1], 'w').write(str(os.getpid())); time.sleep(600)",
                    {str(pidfile)!r}])
                time.sleep(600)
        """,
    })
    r = run(cfg, p, timeout=15)
    assert r["outcome"] == "timeout"
    pid = int(wait_file(pidfile))
    assert wait_dead(pid), "the process started by the test survived the timeout"


def test_daemon_left_by_a_finished_test_is_killed(cfg, tmp_path):
    pidfile = tmp_path / "daemon.pid"
    p = write_project(tmp_path / "proj", {
        "tests/test_d.py": f"""
            import subprocess, sys


            def test_leaves_a_server_running():
                subprocess.Popen([sys.executable, "-c",
                    "import os, time, sys; open(sys.argv[1], 'w').write(str(os.getpid())); time.sleep(600)",
                    {str(pidfile)!r}])
        """,
    })
    r = run(cfg, p)
    assert r["outcome"] == "passed"
    pid = int(wait_file(pidfile))
    assert wait_dead(pid), "a daemon left by the tests was not cleaned up"


def test_process_killed_by_os_exit_is_reported_as_crash(cfg, tmp_path):
    p = write_project(tmp_path / "proj", {
        "tests/test_c.py": """
            import os


            def test_fine():
                assert True


            def test_dies():
                os._exit(7)
        """,
    })
    r = run(cfg, p)
    assert r["outcome"] == "crashed"
    assert r["exit_code"] == 7
    assert r["running_when_stopped"] == "tests/test_c.py::test_dies"
    assert r["finished_before_stop"] == {"passed": 1}


def test_huge_output_is_trimmed(cfg, tmp_path):
    p = write_project(tmp_path / "proj", {
        "tests/test_big.py": """
            def test_noisy_failure():
                for i in range(40000):
                    print("noise line %d " % i + "y" * 60)
                print("LAST LINE BEFORE FAILURE")
                assert False, "boom"


            def test_noisy_pass():
                for i in range(20000):
                    print("more noise " * 10)
        """,
    })
    r = run(cfg, p)
    assert r["summary"]["passed"] == 1 and r["summary"]["failed"] == 1
    out = r["failures"][0]["stdout"]
    assert len(out) < 1700 and out.startswith("[... ") and out.endswith("LAST LINE BEFORE FAILURE")
    assert len(json.dumps(r)) < 8000


def test_many_failures_are_capped_but_counted(cfg, tmp_path):
    body = "\n".join(f"def test_f{i:03d}(): assert {i} == -1" for i in range(45))
    p = write_project(tmp_path / "proj", {"tests/test_many.py": body + "\n"})
    r = run(cfg, p)
    assert r["summary"]["failed"] == 45
    assert len(r["failures"]) == 20
    assert r["failures_not_shown"]["count"] == 25
    assert len(r["failures_not_shown"]["ids"]) == 25
    assert len(json.dumps(r)) < 20000


def test_strict_xpass_is_a_failure(cfg, tmp_path):
    p = write_project(tmp_path / "proj", {
        "tests/test_x.py": """
            import pytest


            @pytest.mark.xfail(strict=True)
            def test_unexpectedly_passes():
                assert True
        """,
    })
    r = run(cfg, p)
    assert r["summary"]["failed"] == 1
    assert "XPASS" in r["failures"][0]["message"]


def test_selector_and_extra_args(cfg, tmp_path):
    p = write_project(tmp_path / "proj", {
        "tests/test_a.py": """
            def test_one(): assert False
            def test_two(): assert False
            def test_three(): assert True
        """,
        "tests/test_b.py": "def test_b(): assert True\n",
    })
    assert run(cfg, p, selector="tests/test_b.py")["summary"]["total"] == 1
    assert run(cfg, p, selector="tests/test_a.py::test_three")["outcome"] == "passed"
    r = run(cfg, p, extra_args=["-k", "two or three"])
    assert r["summary"]["total"] == 2 and r["summary"]["failed"] == 1
    r = run(cfg, p, extra_args=["-x"])
    assert r["summary"]["failed"] == 1 and r["summary"]["total"] == 1
    r = run(cfg, p, extra_args=["--maxfail", "2"])
    assert r["summary"]["failed"] == 2


def test_dangerous_arguments_are_refused_before_anything_runs(cfg, tmp_path):
    marker = tmp_path / "pwned.txt"
    p = write_project(tmp_path / "proj", {
        "evil.py": f"open({str(marker)!r}, 'w').write('x')\n",
        "tests/test_a.py": "def test_a(): pass\n",
    })
    with pytest.raises(TestrunError, match="not allowed"):
        run(cfg, p, extra_args=["-p", "evil"])
    with pytest.raises(TestrunError, match="starts with '-'"):
        run(cfg, p, selector="--co")
    with pytest.raises(TestrunError, match="outside the project"):
        run(cfg, p, selector="../../etc/passwd")
    with pytest.raises(TestrunError, match="timeout"):
        run(cfg, p, timeout=0)
    assert not marker.exists()


def test_missing_pytest_names_the_interpreter(cfg, tmp_path, bare_venv):
    from conftest import venv_python

    py = venv_python(bare_venv)
    p = write_project(tmp_path / "proj", {"tests/test_a.py": "def test_a(): pass\n"})
    with pytest.raises(TestrunError) as ei:
        run(cfg, p, python=str(py))
    msg = str(ei.value)
    assert "pytest" in msg and str(py) in msg and "python=" in msg


def test_project_venv_is_detected(cfg, tmp_path, bare_venv):
    import shutil

    from conftest import venv_python

    p = write_project(tmp_path / "proj", {"tests/test_a.py": "def test_a(): pass\n"})
    shutil.copytree(bare_venv, p / ".venv", symlinks=True)
    local = venv_python(p / ".venv")
    # A copied venv is not relocatable on every OS, so only check what is detected,
    # and that the failure message names the project venv interpreter.
    from testrun_mcp.pyenv import find_interpreter

    interp = find_interpreter(p, None)
    assert interp.source == "project venv" and interp.argv == [str(local)]
    d = service.detect(cfg, str(p), None)
    assert "project venv" in d["python"]["source"]


def test_bad_python_argument(cfg, tmp_path):
    p = write_project(tmp_path / "proj", {"tests/test_a.py": "def test_a(): pass\n"})
    with pytest.raises(TestrunError, match="not an existing file"):
        run(cfg, p, python=str(tmp_path / "nope" / "python"))
    notpy = tmp_path / "evil.exe"
    notpy.write_text("x")
    with pytest.raises(TestrunError, match="named python"):
        run(cfg, p, python=str(notpy))


def test_env_is_scrubbed_inside_tests(cfg, tmp_path, monkeypatch):
    monkeypatch.setenv("TESTRUN_FAKE_SECRET", "s3cret")
    p = write_project(tmp_path / "proj", {
        "tests/test_env.py": """
            import os


            def test_secret_is_hidden():
                assert "TESTRUN_FAKE_SECRET" not in os.environ


            def test_unbuffered_utf8():
                assert os.environ["PYTHONIOENCODING"] == "utf-8"
        """,
    })
    assert run(cfg, p)["outcome"] == "passed"
    from testrun_mcp.config import Config

    visible = write_project(tmp_path / "proj2", {
        "tests/test_env.py": """
            import os


            def test_secret_is_visible():
                assert os.environ["TESTRUN_FAKE_SECRET"] == "s3cret"
        """,
    })
    assert run(Config(allow_env=frozenset({"TESTRUN_FAKE_SECRET"})), visible)["outcome"] == "passed"
    assert run(Config(scrub_env=False), visible)["outcome"] == "passed"
    assert run(cfg, visible)["outcome"] == "failed"


def test_rerun_failed_lifecycle(cfg, tmp_path):
    p = write_project(tmp_path / "proj", {
        "tests/test_a.py": """
            def test_ok(): assert True
            def test_bad1(): assert 1 == 2
            def test_bad2(): assert "a" == "b"
        """,
    })
    with pytest.raises(TestrunError, match="call run_tests first"):
        service.rerun_failed(cfg, str(p), 60, None)
    first = run(cfg, p)
    assert first["summary"]["failed"] == 2 and first["summary"]["passed"] == 1
    second = service.rerun_failed(cfg, str(p), 60, None)
    assert second["summary"]["total"] == 2 and second["summary"]["failed"] == 2
    assert second["rerun_ids"] == 2
    # Fix one of them; only the other should remain.
    (p / "tests" / "test_a.py").write_text(
        "def test_ok(): assert True\ndef test_bad1(): assert True\n"
        "def test_bad2(): assert 'a' == 'b'\n", encoding="utf-8", newline="\n")
    third = service.rerun_failed(cfg, str(p), 60, None)
    assert third["summary"]["total"] == 2 and third["summary"]["passed"] == 1
    assert [x["id"] for x in third["failures"]] == ["tests/test_a.py::test_bad2"]
    (p / "tests" / "test_a.py").write_text("def test_bad2(): assert True\n", encoding="utf-8")
    fourth = service.rerun_failed(cfg, str(p), 60, None)
    assert fourth["outcome"] == "passed"
    assert service.rerun_failed(cfg, str(p), 60, None)["outcome"] == "nothing_to_rerun"


def test_rerun_failed_includes_collection_errors(cfg, tmp_path):
    p = write_project(tmp_path / "proj", {
        "tests/test_bad.py": "import nope_nope_xyz\n",
        "tests/test_ok.py": "def test_ok(): pass\n",
    })
    assert run(cfg, p)["summary"]["collection_errors"] == 1
    again = service.rerun_failed(cfg, str(p), 60, None)
    assert again["summary"]["collection_errors"] == 1 and again["summary"]["total"] == 0


def test_list_tests(cfg, tmp_path):
    p = write_project(tmp_path / "proj", {
        "tests/test_a.py": """
            import pytest


            class TestX:
                def test_one(self): pass
                def test_two(self): pass


            @pytest.mark.parametrize("v", [1, "ü"])
            def test_param(v): pass
        """,
        "tests/test_bad.py": "import not_a_real_module_zzz\n",
    })
    r = service.list_tests(cfg, str(p), None, None, 60, None, None)
    assert r["runner"] == "pytest" and r["count"] == 4
    assert r["tests"] == [
        "tests/test_a.py::TestX::test_one",
        "tests/test_a.py::TestX::test_two",
        "tests/test_a.py::test_param[1]",
        "tests/test_a.py::test_param[\\xfc]",
    ]
    assert len(r["collection_errors"]) == 1 and "tests/test_bad.py" in r["collection_errors"][0]
    r = service.list_tests(cfg, str(p), None, "two", 60, None, None)
    assert r["tests"] == ["tests/test_a.py::TestX::test_two"]
    r = service.list_tests(cfg, str(p), "tests/test_a.py::TestX", None, 60, None, None)
    assert r["count"] == 2
    empty = write_project(tmp_path / "empty", {"tests/test_a.py": "x = 1\n"})
    assert service.list_tests(cfg, str(empty), None, None, 60, None, "pytest")["note"] == "no tests collected"


def test_flaky_check_with_counter_file(cfg, tmp_path):
    counter = tmp_path / "counter.txt"
    p = write_project(tmp_path / "proj", {
        "tests/test_flaky.py": f"""
            import os
            import pathlib

            COUNTER = pathlib.Path({str(counter)!r})


            def test_flaky():
                n = int(COUNTER.read_text()) if COUNTER.exists() else 0
                COUNTER.write_text(str(n + 1))
                assert n % 2 == 0, "failed on odd run %d" % n


            def test_stable():
                assert True


            def test_broken():
                assert False
        """,
    })
    r = service.flaky_check(cfg, str(p), "tests/test_flaky.py::test_flaky", 5, 60, None, None)
    assert r["pattern"] == "PFPFP"
    assert r["verdict"] == "flaky" and r["flaky"] is True
    assert r["failure_rate"] == 0.4 and r["counts"] == {"P": 3, "F": 2}
    assert r["first_failure"]["message"].startswith("AssertionError: failed on odd run 1")
    assert len(r["run_seconds"]) == 5

    r = service.flaky_check(cfg, str(p), "tests/test_flaky.py::test_stable", 3, 60, None, None)
    assert r["pattern"] == "PPP" and r["verdict"] == "stable_pass" and "flaky" not in r
    r = service.flaky_check(cfg, str(p), "tests/test_flaky.py::test_broken", 3, 60, None, None)
    assert r["pattern"] == "FFF" and r["verdict"] == "consistently_failing"


def test_flaky_check_errors(cfg, tmp_path):
    p = write_project(tmp_path / "proj", {"tests/test_a.py": "def test_a(): pass\n"})
    with pytest.raises(TestrunError, match="between 2 and 20"):
        service.flaky_check(cfg, str(p), "tests/test_a.py::test_a", 1, 60, None, None)
    with pytest.raises(TestrunError, match="not found"):
        service.flaky_check(cfg, str(p), "tests/test_a.py::test_nope", 2, 60, None, None)


def test_flaky_check_counts_timeouts(cfg, tmp_path):
    counter = tmp_path / "c.txt"
    p = write_project(tmp_path / "proj", {
        "tests/test_t.py": f"""
            import pathlib, time

            C = pathlib.Path({str(counter)!r})


            def test_sometimes_hangs():
                n = int(C.read_text()) if C.exists() else 0
                C.write_text(str(n + 1))
                if n == 1:
                    time.sleep(600)
        """,
    })
    r = service.flaky_check(cfg, str(p), "tests/test_t.py::test_sometimes_hangs", 3, 10, None, None)
    assert r["pattern"] == "PTP" and r["verdict"] == "flaky"


def test_explain_failure_includes_locals(cfg, tmp_path):
    p = write_project(tmp_path / "proj", {
        "tests/test_l.py": """
            def compute(items):
                total = 0
                secret_marker = 424242
                for it in items:
                    total += it
                return total / (len(items) - len(items))


            def test_with_locals():
                data = [1, 2, 3]
                print("captured text here")
                compute(data)


            def test_passes():
                pass
        """,
    })
    r = service.explain_failure(cfg, str(p), "tests/test_l.py::test_with_locals", 60, None, None)
    assert r["outcome"] == "failed" and r["message"] == "ZeroDivisionError: division by zero"
    assert "secret_marker = 424242" in r["traceback"]
    assert "data" in r["traceback"] and "total" in r["traceback"]
    assert r["stdout"] == "captured text here"
    assert r["location"] == "tests/test_l.py:6"
    ok = service.explain_failure(cfg, str(p), "tests/test_l.py::test_passes", 60, None, None)
    assert ok["outcome"] == "passed" and "did not fail" in ok["note"]
    with pytest.raises(TestrunError, match="not found"):
        service.explain_failure(cfg, str(p), "tests/test_l.py::nothing", 60, None, None)


def test_explain_failure_for_a_collection_error(cfg, tmp_path):
    p = write_project(tmp_path / "proj", {"tests/test_bad.py": "import no_such_thing_abc\n"})
    r = service.explain_failure(cfg, str(p), "tests/test_bad.py", 60, None, None)
    assert r["outcome"] == "error" and "no_such_thing_abc" in r["traceback"]


def test_coverage_summary(cfg, tmp_path):
    pytest.importorskip("coverage")
    p = write_project(tmp_path / "proj", {
        "src/calc.py": """
            def add(a, b):
                return a + b


            def sub(a, b):
                x = a - b
                return x


            def never(a):
                if a:
                    return 1
                return 2
        """,
        "src/other.py": """
            def used():
                return 1
        """,
        "tests/test_calc.py": """
            from src.calc import add
            from src.other import used


            def test_add():
                assert add(1, 2) == 3
                assert used() == 1
        """,
    })
    r = service.coverage_summary(cfg, str(p), None, None, 120, None, None)
    files = by_id([{**f, "id": f["file"]} for f in r["files"]])
    assert files["src/calc.py"]["statements"] == 9
    assert files["src/calc.py"]["uncovered_lines"] == "6-7, 11-13"
    assert files["src/calc.py"]["percent"] == 44.4
    assert files["src/other.py"]["percent"] == 100.0 and files["src/other.py"]["uncovered_lines"] == ""
    assert r["files"][0]["file"] == "src/calc.py"  # least covered first
    assert r["tests_exit_code"] == 0
    assert not (p / ".coverage").exists()  # nothing written into the project
    only = service.coverage_summary(cfg, str(p), ["src/calc.py"], None, 120, None, None)
    assert [f["file"] for f in only["files"]] == ["src/calc.py"]
    glob = service.coverage_summary(cfg, str(p), ["src/o*.py"], None, 120, None, None)
    assert [f["file"] for f in glob["files"]] == ["src/other.py"]
    with pytest.raises(TestrunError, match="matched"):
        service.coverage_summary(cfg, str(p), ["src/missing.py"], None, 120, None, None)


def test_coverage_missing_names_interpreter(cfg, tmp_path, bare_venv):
    from conftest import venv_python

    py = venv_python(bare_venv)
    p = write_project(tmp_path / "proj", {"tests/test_a.py": "def test_a(): pass\n"})
    with pytest.raises(TestrunError) as ei:
        service.coverage_summary(cfg, str(p), None, None, 60, str(py), "pytest")
    assert "coverage" in str(ei.value) and str(py) in str(ei.value)


def test_works_with_cwd_that_is_not_the_project(cfg, tmp_path, monkeypatch):
    p = write_project(tmp_path / "proj", {"tests/test_a.py": "def test_a(): assert True\n"})
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert run(cfg, p)["outcome"] == "passed"
    assert os.getcwd() == str(elsewhere)


def test_rerun_skips_ids_whose_files_were_deleted(cfg, tmp_path):
    p = write_project(tmp_path / "proj", {
        "tests/test_bad.py": "import nope_nope_xyz\n",
        "tests/test_fail.py": "def test_f(): assert False\n",
    })
    assert run(cfg, p)["summary"]["collection_errors"] == 1
    (p / "tests" / "test_bad.py").unlink()
    again = service.rerun_failed(cfg, str(p), 60, None)
    assert again["summary"]["failed"] == 1 and again["summary"]["collection_errors"] == 0
    assert any("no longer exist" in n for n in again["notes"])
    (p / "tests" / "test_fail.py").unlink()
    gone = service.rerun_failed(cfg, str(p), 60, None)
    assert gone["outcome"] == "nothing_to_rerun" and "no longer exist" in gone["message"]
