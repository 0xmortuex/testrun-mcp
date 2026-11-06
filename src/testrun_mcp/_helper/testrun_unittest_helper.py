"""Run unittest in the target project's interpreter and write a JSON report.

Stdlib only. Lives in its own directory so that putting its folder on sys.path
(which Python does for the script being run) cannot shadow project modules.
"""

import argparse
import json
import os
import sys
import time
import traceback
import unittest


def flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from flatten(item)
        else:
            yield item


FAILED_PREFIX = "unittest.loader._FailedTest."


def test_id(test):
    ident = test.id()
    if type(test).__name__ == "_FailedTest" and ident.startswith(FAILED_PREFIX):
        ident = ident[len(FAILED_PREFIX):]
    return ident


class Recorder(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.records = []
        self._t0 = 0.0

    def startTest(self, test):
        self._t0 = time.perf_counter()
        super().startTest(test)

    def _add(self, test, outcome, exc_info=None, reason="", sub=""):
        rec = {
            "id": test_id(test) + sub,
            "outcome": outcome,
            "time": time.perf_counter() - self._t0,
            "reason": reason,
            "collection": type(test).__name__ == "_FailedTest",
        }
        if exc_info is not None:
            etype, evalue, tb = exc_info
            rec["message"] = "".join(traceback.format_exception_only(etype, evalue)).strip()
            rec["frames"] = [
                {"file": f.filename, "line": f.lineno, "func": f.name, "code": f.line or ""}
                for f in traceback.extract_tb(tb)
            ]
            rec["body"] = self._exc_info_to_string(exc_info, test)
        self.records.append(rec)

    def addSuccess(self, test):
        super().addSuccess(test)
        self._add(test, "passed")

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._add(test, "failed", err)

    def addError(self, test, err):
        super().addError(test, err)
        self._add(test, "error", err)

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self._add(test, "skipped", reason=reason)

    def addExpectedFailure(self, test, err):
        super().addExpectedFailure(test, err)
        self._add(test, "xfailed")

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self._add(test, "xpassed")

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            kind = "failed" if issubclass(err[0], test.failureException) else "error"
            self._add(test, kind, err, sub=" " + subtest._subDescription())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("-f", "--failfast", action="store_true")
    ap.add_argument("-k", action="append")
    ap.add_argument("targets", nargs="*")
    a = ap.parse_args()
    sys.path.insert(0, os.getcwd())
    loader = unittest.TestLoader()
    if a.k:
        loader.testNamePatterns = [p if "*" in p else "*%s*" % p for p in a.k]
    if a.targets:
        suite = loader.loadTestsFromNames(a.targets)
    else:
        suite = loader.discover(".", top_level_dir=".")
    if a.list:
        ids = [{"id": test_id(t), "collection": type(t).__name__ == "_FailedTest"}
               for t in flatten(suite)]
        with open(a.out, "w", encoding="utf-8") as fh:
            json.dump({"tests": ids}, fh)
        return 0
    runner = unittest.TextTestRunner(
        stream=sys.stderr, verbosity=2, resultclass=Recorder, failfast=a.failfast, buffer=True
    )
    result = runner.run(suite)
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump({"cases": result.records}, fh)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
