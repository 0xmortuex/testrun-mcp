import { x } from "./does-not-exist.js";
import { test } from "vitest";
test("never runs", () => {});
