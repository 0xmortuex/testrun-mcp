import { describe, expect, test } from "vitest";
import { sum } from "./sum.js";

describe("sum", () => {
  test("adds numbers", () => {
    expect(sum(1, 2)).toBe(3);
  });
  test("is wrong on purpose", () => {
    expect(sum(1, 2)).toBe(4);
  });
  test.skip("skipped one", () => {});
  test.todo("write more");
});
