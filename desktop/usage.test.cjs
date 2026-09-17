"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { parseUsage } = require("./usage.cjs");

test("reports an unconfigured source without inventing a quota", () => {
  assert.deepEqual(parseUsage({ id: "codex" }, {}), { state: "not-configured", detail: "No usage adapter configured" });
});

test("normalizes declared used and limit fields", () => {
  const result = parseUsage({ usage: { usedField: "used", limitField: "limit" } }, { used: 18, limit: 50 });
  assert.equal(result.state, "ready");
  assert.equal(result.remaining, 32);
});
