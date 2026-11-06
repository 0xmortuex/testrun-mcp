"""MCP stdio smoke test: spawns the real server over stdio, checks the tool list, runs
tests in a real temp project through it, and checks that instructive errors reach the
agent (MCPServer hides any exception that isn't a ToolError)."""

from __future__ import annotations

import json
import sys

import pytest
from _util import write_project

pytest.importorskip("mcp")

from mcp import ClientSession  # noqa: E402
from mcp.client.stdio import StdioServerParameters, stdio_client  # noqa: E402

EXPECTED_TOOLS = {
    "detect_runner",
    "run_tests",
    "rerun_failed",
    "list_tests",
    "flaky_check",
    "explain_failure",
    "coverage_summary",
}

PROJECT = {
    "tests/test_a.py": """
        def test_ok():
            assert True


        def test_bad():
            assert 1 == 2, "one is not two"
    """,
}


def params(*extra: str, env: dict[str, str] | None = None) -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable, args=["-m", "testrun_mcp", *extra], env=env)


def payload(res) -> dict:
    assert res.content, "empty tool result"
    return json.loads(res.content[0].text)


@pytest.mark.anyio
async def test_tools_and_a_session_over_stdio(tmp_path):
    project = write_project(tmp_path / "proj", PROJECT)
    async with stdio_client(params()) as (read, write), ClientSession(read, write) as session:
        init = await session.initialize()
        assert init.server_info.name == "testrun"
        tools = (await session.list_tools()).tools
        assert {t.name for t in tools} == EXPECTED_TOOLS
        for t in tools:
            assert t.description and len(t.description) > 80, t.name

        res = await session.call_tool("detect_runner", {"path": str(project)})
        assert not res.is_error and payload(res)["primary"] == "pytest"

        res = await session.call_tool("run_tests", {"path": str(project)})
        assert not res.is_error
        out = payload(res)
        assert out["outcome"] == "failed" and out["summary"]["passed"] == 1
        assert out["failures"][0]["id"] == "tests/test_a.py::test_bad"
        assert out["failures"][0]["message"].startswith("AssertionError: one is not two")

        res = await session.call_tool("rerun_failed", {"path": str(project)})
        assert payload(res)["summary"]["total"] == 1

        res = await session.call_tool("list_tests", {"path": str(project)})
        assert payload(res)["count"] == 2

        res = await session.call_tool(
            "flaky_check", {"path": str(project), "test_id": "tests/test_a.py::test_ok", "n": 2})
        assert payload(res)["pattern"] == "PP"

        res = await session.call_tool(
            "explain_failure", {"path": str(project), "test_id": "tests/test_a.py::test_bad"})
        assert "one is not two" in payload(res)["traceback"]


@pytest.mark.anyio
async def test_instructive_errors_reach_the_agent(tmp_path):
    project = write_project(tmp_path / "proj", PROJECT)
    async with stdio_client(params()) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        res = await session.call_tool("run_tests", {"path": str(tmp_path / "missing")})
        assert res.is_error and "does not exist" in res.content[0].text
        res = await session.call_tool(
            "run_tests", {"path": str(project), "extra_args": ["-p", "evil"]})
        assert res.is_error and "not allowed" in res.content[0].text and "Allowed flags" in res.content[0].text
        res = await session.call_tool("rerun_failed", {"path": str(project)})
        assert res.is_error and "call run_tests first" in res.content[0].text
        res = await session.call_tool("run_tests", {"path": str(project), "timeout": 0})
        assert res.is_error and "timeout must be between" in res.content[0].text


@pytest.mark.anyio
async def test_dir_flag_sandboxes_projects(tmp_path):
    inside = write_project(tmp_path / "box" / "proj", PROJECT)
    outside = write_project(tmp_path / "other", PROJECT)
    async with stdio_client(params("--dir", str(tmp_path / "box"))) as (read, write), \
            ClientSession(read, write) as session:
        await session.initialize()
        res = await session.call_tool("run_tests", {"path": str(inside)})
        assert not res.is_error
        res = await session.call_tool("run_tests", {"path": str(outside)})
        assert res.is_error and "outside the sandbox" in res.content[0].text


@pytest.mark.anyio
async def test_environment_flags(tmp_path):
    # The client gives the *server* process this variable; the server decides whether
    # the test processes it launches get to see it.
    project = write_project(tmp_path / "proj", {
        "tests/test_env.py": """
            import os


            def test_secret():
                assert os.environ.get("TESTRUN_E2E_SECRET") == "abc"
        """,
    })

    async def outcome(*flags: str) -> str:
        async with stdio_client(params(*flags, env={"TESTRUN_E2E_SECRET": "abc"})) as (read, write), \
                ClientSession(read, write) as session:
            await session.initialize()
            res = await session.call_tool("run_tests", {"path": str(project)})
            return payload(res)["outcome"]

    assert await outcome() == "failed"  # scrubbed by default
    assert await outcome("--allow-env", "TESTRUN_E2E_SECRET") == "passed"
    assert await outcome("--inherit-env") == "passed"


@pytest.mark.anyio
async def test_a_long_run_does_not_block_other_requests(tmp_path):
    """Tests run in a worker thread, so the server still answers while they run."""
    import anyio

    project = write_project(tmp_path / "proj", {
        "tests/test_slow.py": """
            import time


            def test_slow():
                time.sleep(3)
        """,
    })
    async with stdio_client(params()) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        results: dict[str, object] = {}

        async def slow() -> None:
            results["slow"] = await session.call_tool("run_tests", {"path": str(project)})

        async with anyio.create_task_group() as tg:
            tg.start_soon(slow)
            await anyio.sleep(1.0)
            quick = await session.list_tools()
            assert len(quick.tools) == 7
            assert "slow" not in results, "list_tools had to wait for the test run"
        assert payload(results["slow"])["outcome"] == "passed"  # type: ignore[arg-type]
