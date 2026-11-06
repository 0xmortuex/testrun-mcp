"""extra_args allow-list, selector validation, and traceback trimming."""

from __future__ import annotations

import pytest

from testrun_mcp.args import check_selector, parse_extra_args
from testrun_mcp.config import Config
from testrun_mcp.errors import TestrunError
from testrun_mcp.trim import (
    Frame,
    clip,
    clip_middle,
    parse_traceback,
    relevant_frames,
    split_captured,
    strip_banners,
)


def test_allowed_pytest_flags_are_normalised():
    assert parse_extra_args("pytest", ["-x"]) == ["-x"]
    assert parse_extra_args("pytest", ["-k", "login and not slow"]) == ["-k", "login and not slow"]
    assert parse_extra_args("pytest", ["--maxfail=3"]) == ["--maxfail=3"]
    assert parse_extra_args("pytest", ["--maxfail", "3", "-m", "not slow"]) == [
        "--maxfail=3", "-m", "not slow",
    ]
    assert parse_extra_args("pytest", None) == []
    assert parse_extra_args("jest", ["-t", "adds numbers", "--bail"]) == ["-t", "adds numbers", "--bail"]
    assert parse_extra_args("vitest", ["--bail=1"]) == ["--bail=1"]
    assert parse_extra_args("unittest", ["-k", "foo", "--failfast"]) == ["-k", "foo", "--failfast"]


@pytest.mark.parametrize(
    "bad",
    [
        ["-p", "evil_plugin"],
        ["-c", "other.ini"],
        ["--rootdir=/etc"],
        ["--basetemp=/tmp/x"],
        ["--junitxml=/tmp/x.xml"],
        ["--override-ini=addopts=-p evil"],
        ["-o", "addopts=x"],
        ["--pyargs"],
        ["--co"],
        ["; rm -rf /"],
        ["&& whoami"],
        ["$(whoami)"],
        ["-x=1"],
    ],
)
def test_pytest_injection_attempts_are_refused(bad):
    with pytest.raises(TestrunError) as ei:
        parse_extra_args("pytest", bad)
    assert "extra_args" in str(ei.value)


@pytest.mark.parametrize(
    "runner,bad",
    [
        ("jest", ["--setupFiles", "evil.js"]),
        ("jest", ["--config", "evil.json"]),
        ("jest", ["--globalSetup=evil.js"]),
        ("vitest", ["--config", "evil.mjs"]),
        ("vitest", ["--root=/"]),
        ("unittest", ["--out", "x"]),
    ],
)
def test_node_and_unittest_injection_refused(runner, bad):
    with pytest.raises(TestrunError):
        parse_extra_args(runner, bad)


@pytest.mark.parametrize(
    "bad",
    [["--maxfail=abc"], ["--maxfail"], ["-k"], ["-k", "-x"], ["--import-mode=x"], ["-k\x00"],
     ["-n", "2"], ["--timeout=5"]],
)
def test_bad_values_are_refused(bad):
    with pytest.raises(TestrunError):
        parse_extra_args("pytest", bad)


def test_selector_rules(tmp_path):
    assert check_selector("pytest", tmp_path, None) is None
    assert check_selector("pytest", tmp_path, "") is None
    assert check_selector("pytest", tmp_path, "tests/test_a.py::T::test[1 2]") == "tests/test_a.py::T::test[1 2]"
    assert check_selector("pytest", tmp_path, "sub/../tests") == "sub/../tests"
    for bad in ("-p evil", "--rootdir=/", "../outside.py", "tests/../../outside"):
        with pytest.raises(TestrunError):
            check_selector("pytest", tmp_path, bad)
    outside = tmp_path.parent / "elsewhere.py"
    with pytest.raises(TestrunError, match="outside the project"):
        check_selector("pytest", tmp_path, str(outside))
    # inside, absolute: fine
    assert check_selector("pytest", tmp_path, str(tmp_path / "t.py"))
    with pytest.raises(TestrunError):
        check_selector("jest", tmp_path, "--config")


def test_sandbox_rejects_paths_outside(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    cfg = Config(sandbox=a.resolve())
    assert cfg.resolve_project(str(a)) == a.resolve()
    with pytest.raises(TestrunError, match="outside the sandbox"):
        cfg.resolve_project(str(b))
    with pytest.raises(TestrunError, match="outside the sandbox"):
        cfg.resolve_project(str(a / ".." / "b"))
    with pytest.raises(TestrunError, match="does not exist"):
        cfg.resolve_project(str(a / "nope"))
    f = a / "file.txt"
    f.write_text("x")
    with pytest.raises(TestrunError, match="not a directory"):
        cfg.resolve_project(str(f))


SHORT_TB = """\
tests\\test_a.py:8: in test_zero
    f(0)
src\\m.py:2: in f
    return 1 / x
           ^^^^^
.venv\\Lib\\site-packages\\pluggy\\_callers.py:103: in _multicall
    res = hook_impl.function(*args)
C:\\Users\\me\\proj\\src\\deep.py:40: in g
    raise RuntimeError("boom")
E   RuntimeError: boom
E   second line
"""


def test_parse_traceback_windows_paths_and_carets():
    frames, e_lines = parse_traceback(SHORT_TB)
    assert [(f.file, f.line, f.func) for f in frames] == [
        ("tests/test_a.py", 8, "test_zero"),
        ("src/m.py", 2, "f"),
        (".venv/Lib/site-packages/pluggy/_callers.py", 103, "_multicall"),
        ("C:/Users/me/proj/src/deep.py", 40, "g"),
    ]
    assert frames[1].code == "return 1 / x"  # the ^^^^ marker line is not code
    assert e_lines == ["RuntimeError: boom", "second line"]


def test_relevant_frames_drops_library_frames():
    frames, _ = parse_traceback(SHORT_TB)
    kept, dropped = relevant_frames(frames)
    assert [f.file for f in kept] == ["tests/test_a.py", "src/m.py", "C:/Users/me/proj/src/deep.py"]
    assert dropped == 1


def test_relevant_frames_caps_long_tracebacks():
    frames = [Frame(f"src/m{i}.py", i, f"f{i}", "") for i in range(30)]
    kept, dropped = relevant_frames(frames)
    assert len(kept) == 5 and kept[0].file == "src/m0.py" and kept[-1].file == "src/m29.py"
    assert dropped == 25


def test_relevant_frames_keeps_last_when_all_are_library():
    frames = [Frame("/x/site-packages/a.py", 1, "a", ""), Frame("/x/site-packages/b.py", 2, "b", "")]
    kept, dropped = relevant_frames(frames)
    assert [f.file for f in kept] == ["/x/site-packages/b.py"] and dropped == 1


def test_clip_keeps_tail_and_reports_what_was_cut():
    text = "".join(f"{i}\n" for i in range(2000))
    out = clip(text, 100)
    assert out.startswith("[... ") and out.endswith("1999") and len(out) < 160
    assert clip("short\n", 100) == "short"


def test_clip_middle_keeps_both_ends():
    out = clip_middle("A" * 500 + "B" * 500, 100)
    assert out.startswith("A" * 50) and out.endswith("B" * 50) and "chars trimmed" in out


def test_split_captured_and_banners():
    body = "tests/t.py:1: in t\n    x\nE boom\n----------- Captured stdout call -----------\nhi\n"
    tb, cap = split_captured(body)
    assert tb.endswith("E boom") and cap.startswith("-----------")
    assert strip_banners("--- Captured Out ---\nhello\n--- Captured Err ---\n") == "hello"
