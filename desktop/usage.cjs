"use strict";

function parseUsage(source, payload) {
  const usage = source.usage;
  if (!usage) return { state: "not-configured", detail: "No usage adapter configured" };
  const value = Number(payload?.[usage.usedField]);
  const limit = Number(payload?.[usage.limitField]);
  if (!Number.isFinite(value) || !Number.isFinite(limit) || limit <= 0) {
    return { state: "unavailable", detail: "Usage response did not contain usable fields" };
  }
  return { state: "ready", used: value, limit, remaining: Math.max(0, limit - value), updatedAt: new Date().toISOString() };
}

module.exports = { parseUsage };
