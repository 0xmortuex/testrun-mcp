"""Real jest and vitest. Skipped unless `npm ci` has been run in the fixture project
(the optional `js` CI job does that; the default run needs no network)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from testrun_mcp import service

FIX = Path(__file__).parent / "fixtures"


def needs(name: str, runner: str) -> pytest.MarkDecorator:
    installed = (FIX / name / "node_modules" / runner).is_dir()
    return pytest.mark.skipif(
        shutil.which("node") is None or not installed,
        reason=f"needs node and `npm ci` in tests/fixtures/{name}",
    )


def run(cfg, project: Path, **kw):
    args = dict(selector=None, timeout=120, extra_args=None, python=None, runner=None)
    args.update(kw)
    return service.run_tests(cfg, str(project), **args)


@needs("js_jest", "jest")
def test_real_jest(cfg):
    p = FIX / "js_jest"
    d = service.detect(cfg, str(p), None)
    assert d["primary"] == "jest" and d["runners"][0]["ready"] is True
    r = run(cfg, p)
    assert r["runner"] == "jest" and r["outcome"] == "failed"
    s = r["summary"]
    assert (s["passed"], s["failed"], s["skipped"], s["collection_errors"]) == (1, 1, 2, 1)
    f = r["failures"][0]
    assert f["id"] == "sum.test.js::sum is wrong on purpose"
    assert f["location"] == "sum.test.js:8"
    assert "Expected: 4" in f["detail"] and "Received: 3" in f["detail"]
    assert r["collection_errors"][0]["id"] == "broken.test.js"
    assert "does-not-exist" in r["collection_errors"][0]["message"]
    only = run(cfg, p, selector="sum.test.js", extra_args=["-t", "adds"])
    # jest reports the tests that -t filtered out as skipped
    assert only["outcome"] == "passed" and only["summary"]["passed"] == 1


@needs("js_jest", "jest")
def test_real_jest_rerun_flaky_explain_list(cfg):
    p = FIX / "js_jest"
    run(cfg, p, selector="sum.test.js")
    again = service.rerun_failed(cfg, str(p), 120, None)
    assert again["rerun_ids"] == 1
    assert [f["id"] for f in again["failures"]] == ["sum.test.js::sum is wrong on purpose"]
    fl = service.flaky_check(cfg, str(p), "sum.test.js::sum adds numbers", 2, 120, None, None)
    assert fl["pattern"] == "PP"
    ex = service.explain_failure(cfg, str(p), "sum.test.js::sum is wrong on purpose", 120, None, None)
    assert ex["outcome"] == "failed" and "Expected: 4" in ex["traceback"]
    ls = service.list_tests(cfg, str(p), None, None, 120, None, None)
    assert sorted(ls["tests"]) == ["broken.test.js", "sum.test.js"]
    assert "FILE" in ls["note"]


@needs("js_vitest", "vitest")
def test_real_vitest(cfg):
    p = FIX / "js_vitest"
    assert service.detect(cfg, str(p), None)["primary"] == "vitest"
    r = run(cfg, p)
    assert r["runner"] == "vitest" and r["outcome"] == "failed"
    s = r["summary"]
    assert (s["passed"], s["failed"], s["skipped"], s["collection_errors"]) == (1, 1, 2, 1)
    f = r["failures"][0]
    assert f["id"] == "sum.test.js::sum is wrong on purpose"
    assert f["location"] == "sum.test.js:9"
    assert "expected 3 to be 4" in f["message"]
    assert r["collection_errors"][0]["id"] == "broken.test.js"
    run(cfg, p, selector="sum.test.js")
    again = service.rerun_failed(cfg, str(p), 120, None)
    assert again["rerun_ids"] == 1 and again["summary"]["failed"] == 1
    fl = service.flaky_check(cfg, str(p), "sum.test.js::sum adds numbers", 2, 120, None, None)
    assert fl["pattern"] == "PP"
