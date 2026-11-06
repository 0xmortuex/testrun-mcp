// A stand-in for the jest CLI used by the default (no-network) test run.
// It speaks just enough of jest's interface: --json --outputFile=F, -t REGEX,
// positional file regexes. Real jest/vitest are covered by test_real_js.py.
const fs = require("fs");
const path = require("path");

const args = process.argv.slice(2);
let out = null;
let namePattern = null;
const filePatterns = [];
for (let i = 0; i < args.length; i++) {
  const a = args[i];
  if (a.startsWith("--outputFile=")) out = a.slice("--outputFile=".length);
  else if (a === "-t" || a === "--testNamePattern") namePattern = new RegExp(args[++i]);
  else if (a.startsWith("-")) continue;
  else filePatterns.push(new RegExp(a));
}

const counterFile = path.join(process.cwd(), "counter.txt");
let n = fs.existsSync(counterFile) ? parseInt(fs.readFileSync(counterFile, "utf8"), 10) : 0;

const suites = {
  "src/math.test.js": [
    ["math adds", "passed"],
    ["math fails", "failed"],
    ["math later", "pending"],
  ],
  "src/other.test.js": [
    ["other works", "passed"],
    ["other flaky", "flaky"],
  ],
  "src/broken.test.js": "broken",
};

if (process.env.FAKE_JEST_HANG) {
  setInterval(() => {}, 1000);
} else {
  main();
}

function main() {

const testResults = [];
let anyFailed = false;
for (const [rel, tests] of Object.entries(suites)) {
  const abs = path.join(process.cwd(), rel);
  if (filePatterns.length && !filePatterns.some((p) => p.test(abs.split(path.sep).join("/")))) continue;
  if (tests === "broken") {
    anyFailed = true;
    testResults.push({
      name: abs, status: "failed", assertionResults: [],
      message: "\u001b[1m  ● Test suite failed to run\u001b[22m\n\n    Cannot find module './nope' from 'src/broken.test.js'\n\n      at Resolver._throwModNotFoundError (node_modules/jest-resolve/build/resolver.js:427:11)\n      at Object.<anonymous> (" + abs + ":1:1)",
    });
    continue;
  }
  const assertionResults = [];
  for (const [fullName, status0] of tests) {
    if (namePattern && !namePattern.test(fullName)) continue;
    let status = status0;
    if (status === "flaky") {
      status = n % 2 === 0 ? "passed" : "failed";
      n += 1;
      fs.writeFileSync(counterFile, String(n));
    }
    const r = { fullName, title: fullName, status, duration: 3, failureMessages: [] };
    if (status === "failed") {
      anyFailed = true;
      r.failureMessages = [
        "Error: expect(received).toBe(expected) // Object.is equality\n\nExpected: 2\nReceived: 3\n" +
        "    at Object.toBe (" + abs + ":7:20)\n" +
        "    at Promise.then.completed (" + process.cwd() + "/node_modules/jest-circus/build/utils.js:298:28)\n" +
        "    at new Promise (<anonymous>)",
      ];
    }
    assertionResults.push(r);
  }
  testResults.push({ name: abs, status: assertionResults.some((r) => r.status === "failed") ? "failed" : "passed", assertionResults });
}
fs.writeFileSync(out, JSON.stringify({ success: !anyFailed, testResults }));
process.exit(anyFailed ? 1 : 0);
}
