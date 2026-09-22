import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { boundedTool, numberedRead } from "../skills/luna-advisor-escalation/scripts/pi_reader.mjs";

test("tool wrappers deny path escape and bound attempts/output", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "pi-reader-test-"));
  try {
    await fs.writeFile(path.join(root, "safe.txt"), "safe");
    let dispatched = 0;
    const tool = boundedTool({ name: "read", async execute(id, args) {
      dispatched++;
      assert.equal(args.path, path.join(root, "safe.txt"));
      return { content: [{ type: "text", text: "x".repeat(6000) }] };
    } }, root, { tool_calls: 0 });
    await assert.rejects(() => tool.execute("1", { path: ".." }), /outside/);
    assert.equal(dispatched, 0);
    const result = await tool.execute("2", { path: "safe.txt" });
    assert.match(result.content[0].text, /Truncated/);
    for (let i = 0; i < 6; i++) await tool.execute("x", { path: "safe.txt" });
    await assert.rejects(() => tool.execute("9", { path: "safe.txt" }), /budget/);
    assert.equal(dispatched, 7);
  } finally {
    await fs.rm(root, { recursive: true });
  }
});

const packageRoot = process.env.PI_READER_PACKAGE ?? path.join(process.env.APPDATA ?? "", "npm/node_modules/@mariozechner/pi-coding-agent");
const piAvailable = await fs.access(path.join(packageRoot, "dist/index.js")).then(() => true, () => false);
test("installed native Pi read preserves exact line numbers with offsets", { skip: !piAvailable }, async () => {
  const pi = await import(pathToFileURL(path.resolve(packageRoot, "dist/index.js")));
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "pi-native-test-"));
  try {
    await fs.writeFile(path.join(root, "sample.py"), "first\nsecond\nthird\n");
    const read = boundedTool(pi.createReadTool(root, { operations: {
      readFile: numberedRead, access: file => fs.access(file),
    } }), root, { tool_calls: 0 });
    const result = await read.execute("test", { path: "sample.py", offset: 2, limit: 1 });
    assert.match(result.content[0].text, /2: second/);
    assert.doesNotMatch(result.content[0].text, /1: first/);
  } finally {
    await fs.rm(root, { recursive: true });
  }
});
