"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");
const { INSTALLERS, gatewayEndpoints, prepareLaunch, powerShellInvocation } = require("./agents.cjs");

test("builds isolated launch profiles without touching agent homes", async () => {
  const launchHome = await fs.mkdtemp(path.join(os.tmpdir(), "subroute-launch-"));
  try {
    const gateway = "http://127.0.0.1:4000/";
    const codex = await prepareLaunch("codex", gateway, launchHome);
    const hermes = await prepareLaunch("hermes", gateway, launchHome);
    const dsh = await prepareLaunch("dsh", gateway, launchHome);
    const codexConfig = await fs.readFile(path.join(launchHome, "codex", "subroute.config.toml"), "utf8");
    const hermesConfig = await fs.readFile(path.join(launchHome, "hermes", "config.yaml"), "utf8");
    const dshPatch = await fs.readFile(path.join(launchHome, "dsh", "subroute.patch.yaml"), "utf8");
    const codexCatalog = JSON.parse(await fs.readFile(path.join(launchHome, "codex", "models.json"), "utf8"));

    assert.match(codexConfig, /wire_api = "responses"/);
    assert.equal(codex.env.CODEX_HOME, path.join(launchHome, "codex"));
    assert.equal(codex.env.TERM, "xterm-256color");
    assert.equal(codexCatalog.models[0].support_verbosity, true);
    assert.equal(codexCatalog.models[0].default_verbosity, "low");
    assert.equal(codexCatalog.models[0].multi_agent_version, "v2");
    assert.match(hermesConfig, /base_url: "http:\/\/127.0.0.1:4000\/v1"/);
    assert.equal(hermes.env.HERMES_HOME, path.join(launchHome, "hermes"));
    assert.match(dshPatch, /subroute\.settings\.yaml/);
    assert.equal(dsh.env.DSH_HOME, path.join(launchHome, "dsh"));
  } finally {
    await fs.rm(launchHome, { recursive: true, force: true });
  }
});

test("uses a PowerShell call expression for Windows batch shims", () => {
  assert.equal(
    powerShellInvocation("C:\\Program Files\\Tools\\agent.cmd", ["--model", "current"]),
    "& 'C:\\Program Files\\Tools\\agent.cmd' '--model' 'current'",
  );
});

test("normalizes gateway endpoints", () => {
  assert.deepEqual(gatewayEndpoints("http://127.0.0.1:4000/"), { origin: "http://127.0.0.1:4000", openai: "http://127.0.0.1:4000/v1" });
  assert.throws(() => gatewayEndpoints("localhost:4000"), /must start/);
});

test("offers a documented Windows installer for every agent", () => {
  assert.deepEqual(Object.keys(INSTALLERS).sort(), ["claude", "codex", "dsh", "hermes", "openclaw", "opencode"]);
  for (const installer of Object.values(INSTALLERS)) assert.ok(installer.command.length > 0);
  assert.match(INSTALLERS.openclaw.command, /https:\/\/openclaw\.ai\/install\.ps1/);
  assert.match(INSTALLERS.openclaw.command, /-NoOnboard/);
  assert.match(INSTALLERS.hermes.command, /-SkipSetup/);
});
