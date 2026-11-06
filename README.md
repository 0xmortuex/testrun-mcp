# testrun-mcp

**Let your AI agent run your tests and read the result without drowning in a 5,000-line log.**

<!-- mcp-name: io.github.0xmortuex/testrun-mcp -->

testrun-mcp is an [MCP](https://modelcontextprotocol.io) server that runs a project's tests and
returns a small, structured answer: counts, the failures with only the traceback frames that
matter, collection errors kept apart from real failures, the slowest tests, and which Python
interpreter was used. It then lets the agent re-run just the failures, explain one in full
(with local variables), check whether a test is flaky, and see which lines are not covered.

> **Running tests executes the project's code.** Test files, `conftest.py`, pytest plugins,
> fixtures and anything imported at collection time all run with your user's permissions.
> Only point this server at code you would run yourself. See [Security](#security).

## What is different from the other test-runner MCP servers

There are already MCP servers that run pytest (for example
[pytest-evidence-mcp](https://pypi.org/project/pytest-evidence-mcp/) and
[mcp-pytest-runner](https://github.com/jwilger/mcp-pytest-runner)). testrun-mcp is built
around the parts that go wrong when an agent runs tests in a loop:

- **Four runners, one interface:** pytest, unittest, jest and vitest.
- **`flaky_check`**: runs one test N times in fresh processes and gives the pass/fail pattern
  (`PPFPP`), a verdict and the failure rate.
- **Hangs and crashes are results, not hangs:** a hard timeout kills the whole process *tree*
  (a Windows Job Object, or a POSIX process group), and the result names the test that was
  running. Tests that leave a daemon behind do not leak it.
- **Safe by default for an unattended agent:** no shell, stdin closed, output capped, an
  allow-list for `extra_args`, selectors confined to the project, secrets scrubbed from the
  environment, an optional `--dir` sandbox.
- **Honest about the environment:** every result says which interpreter ran the tests; if
  pytest is missing there, the error says how to point at the project's virtualenv.
- **Collection errors are separate from failures**, so an import error never looks like a
  failing assertion.

## Install

Claude Code:

```
claude mcp add testrun -- uvx testrun-mcp
```

Claude Desktop (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "testrun": {
      "command": "uvx",
      "args": ["testrun-mcp"]
    }
  }
}
```

With a sandbox and one allowed environment variable:

```json
{
  "mcpServers": {
    "testrun": {
      "command": "uvx",
      "args": ["testrun-mcp", "--dir", "C:/code", "--allow-env", "DATABASE_URL"]
    }
  }
}
```

Needs Python 3.10+. For jest/vitest you also need `node` on `PATH` and the project's
`node_modules` installed (the server never installs packages). `coverage_summary` needs
`coverage` installed in the project's Python environment.

## What it looks like

A project with one failing test, as returned by `run_tests` (trimmed):

```json
{
  "runner": "pytest",
  "interpreter": "C:\\proj\\.venv\\Scripts\\python.exe (project venv)",
  "outcome": "failed",
  "summary": {"passed": 41, "failed": 1, "error": 0, "skipped": 2, "xfailed": 0, "xpassed": 0,
              "total": 44, "collection_errors": 0},
  "duration_s": 3.1,
  "failures": [{
    "id": "tests/test_cart.py::TestCart::test_total",
    "kind": "failure",
    "message": "assert 19.0 == 20.0\n +  where 19.0 = total()",
    "location": "src/cart.py:27",
    "frames": [
      {"file": "tests/test_cart.py", "line": 14, "in": "test_total", "code": "assert total() == 20.0"},
      {"file": "src/cart.py", "line": 27, "in": "total", "code": "return round(subtotal - discount, 2)"}
    ],
    "stdout": "cart has 3 items"
  }],
  "slowest": [{"id": "tests/test_io.py::test_big_file", "seconds": 1.2}],
  "next": "rerun_failed, or explain_failure(test_id) for the full traceback"
}
```

When a test hangs:

```json
{"outcome": "timeout", "running_when_stopped": "tests/test_net.py::test_connect",
 "finished_before_stop": {"passed": 12}, "output_tail": "..."}
```

## Tools

| Tool | What it does |
|---|---|
| `detect_runner(path)` | Which runner the project uses, the evidence, how it would be run, whether it is ready, which interpreter and which pytest/coverage versions it has. Runs none of the tests. |
| `run_tests(path, selector, timeout, extra_args, python, runner)` | Run the tests. Counts, failures with trimmed tracebacks, collection errors, slowest tests. |
| `rerun_failed(path)` | Re-run only what failed in the last run for that project. |
| `list_tests(path, selector, filter)` | Collect without running: ids and collection errors. |
| `flaky_check(path, test_id, n)` | Run one test 2-20 times, report the pattern and a verdict. |
| `explain_failure(path, test_id)` | Re-run one test with the long traceback and `--showlocals`; the full detail. |
| `coverage_summary(path, files, selector)` | Per-file percent and uncovered line ranges (Python, needs `coverage`). |

Test ids are what the tools print: pytest `tests/test_a.py::TestX::test_y[param]`, unittest
`pkg.test_mod.Class.test_name`, jest/vitest `path/to/file.test.js::full test name`.

`extra_args` accepts only an allow-list of flags (for pytest: `-x`, `--maxfail=N`, `-k`, `-m`,
`--deselect`, `--ignore`, `--runxfail`, `--strict-markers`, `-l`, `--durations=N`, `-W`,
`--import-mode`, `--disable-warnings`). Anything else is refused with the list of
what is allowed.

## Try asking

- "Run the tests in C:/code/myapp and tell me what is broken."
- "Which tests are failing? Show me only the first one in detail, with local variables."
- "Fix that, then re-run only the failures."
- "Is `tests/test_sync.py::test_retry` flaky? Run it 10 times."
- "The test run hangs. Which test is it?"
- "List the tests whose name contains `parser`."
- "What lines of `src/parser.py` are not covered by the tests?"
- "What test runner does this project use and is it set up?"

## Which Python runs the tests

The server's own interpreter almost never has your project's dependencies, so for Python
projects the order is: the `python=` argument (a path to an interpreter, or `uv`, which runs
`uv run --frozen python`), then a virtualenv inside the project (`.venv`, `venv`, `env`,
`.env`), then the interpreter the server itself runs on. The result always says which one was
used. If pytest is not installed there you get an error that names the interpreter and what to
do, rather than a confusing failure.

## Security

**This server runs arbitrary project code by nature.** What it does and does not do:

- **No shell.** Every process is started from an argument list; nothing is interpreted by
  `cmd.exe` or `sh`. Stdin is closed, so a test cannot wait for input.
- **Hard timeout with process-tree kill.** On Windows the test process is placed in a Job
  Object (`KILL_ON_JOB_CLOSE`), so every descendant dies at the timeout, at the end of the
  run, and if the server itself dies. On Linux and macOS it runs in its own session and the
  whole process group gets `SIGKILL`. A descendant that deliberately calls `setsid()`
  escapes the POSIX kill, and on POSIX test processes are not stopped if the server is
  killed outright.
- **Output caps.** At most 32 KiB from the start and 96 KiB from the end of the output are
  kept in memory; the rest is counted and dropped. The result text is bounded too (20
  failures, 1.5 KB of output each, 5 traceback frames each).
- **`extra_args` allow-list and selector checks.** Flags such as `-p`, `-c`, `--rootdir`,
  `--junitxml`, `--config` and `--setupFiles` are refused because they load code or move
  results. A selector may not start with `-` and, for pytest, may not point outside the
  project.
- **Environment scrubbing (on by default).** Test processes get only a short list of basics
  (`PATH`, `HOME`, `TEMP`, `SYSTEMROOT`, locale, ...). API keys and tokens in the server's
  environment do not reach the tests. The operator opens variables with
  `--allow-env NAME` (repeatable) or turns scrubbing off with `--inherit-env`. These are
  server flags, not tool arguments, so an agent cannot widen them.
- **`--dir DIR` sandbox.** Only projects inside `DIR` can be tested. This is a path check on
  the `path` argument. It is **not** a container: the tests themselves can read and write
  anywhere your user can. Use a VM, container or separate account for code you do not trust.
- **`python=`** must name an existing interpreter (`python*` or `pypy*`) so the option cannot
  be used to launch arbitrary programs.
- The server never installs packages and never writes into the project (reports go to a
  temporary directory; pytest's cache plugin is disabled).

Not covered: tests that deliberately escape (network access, writing files, `setsid`), and
code that runs at import time in `conftest.py` before any of this matters.

## Limitations

- pytest ids and counts come from JUnit XML. A project whose `addopts` makes pytest quieter
  (`-q`) can hide the per-test lines used to name a hung test; the timeout still works.
- `list_tests` ignores the project's `addopts` so the output format is predictable.
- jest and vitest can only be listed per file; a test id is `file::full name`.
- `coverage_summary` is Python only and runs the tests under `coverage run`.
- `flaky_check` runs a test in isolation; ordering- or parallelism-dependent flakiness needs
  `run_tests` on the surrounding module.
- The "last run" that `rerun_failed` uses lives in the server's memory, per project.

## Development

```
uv sync --extra test
uv run pytest tests
uv run --with ruff==0.16.0 --with mypy==2.3.0 ruff check src tests
uv run --with ruff==0.16.0 --with mypy==2.3.0 mypy --strict src
```

The tests build real temporary projects (passing, failing, flaky via a counter file, hanging,
import errors, huge output) and run real pytest, unittest and node against them, including a
proof that a grandchild process is dead after a timeout. Real jest and vitest run when you
`npm ci` in `tests/fixtures/js_jest` and `tests/fixtures/js_vitest` (the `real-js` CI job).

Want to build this yourself? [testrun-mcp-the-hard-way](https://github.com/0xmortuex/testrun-mcp-the-hard-way)
is a seven-module tutorial that builds a minimal version from scratch.

MIT licensed.
