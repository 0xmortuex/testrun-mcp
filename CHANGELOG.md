# Changelog

## 0.1.0 - 2026-11-06

First release.

- Seven tools: `detect_runner`, `run_tests`, `rerun_failed`, `list_tests`, `flaky_check`,
  `explain_failure`, `coverage_summary`.
- Runners: pytest (JUnit XML), stdlib unittest (bundled JSON helper), jest and vitest (their
  JSON reporters, run through `node`, never `npx`).
- Results: summary counts, per-failure id / message / innermost project `file:line` / the
  relevant traceback frames only / trimmed stdout and stderr, slowest tests, collection errors
  kept apart from failures, skip reasons, the interpreter that was used.
- Timeouts and crashes name the test that was running. Hard timeout kills the whole process
  tree (Job Object on Windows, process group on POSIX).
- Safety: no shell, stdin closed, output capped, `extra_args` allow-list, selector checks,
  environment scrubbing by default, `--dir` sandbox, `--allow-env`, `--inherit-env`.
