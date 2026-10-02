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
    assert.equal(codexCatalog.models[0].context_window, 256000);
    assert.equal(codexCatalog.models[0].max_context_window, 256000);
    assert.equal(codexCatalog.models[0].effective_context_window_percent, 100);
    assert.equal(codexCatalog.models[0].auto_compact_token_limit, 204800);
    assert.match(codexConfig, /model_context_window = 256000/);
    assert.match(codexConfig, /model_auto_compact_token_limit = 204800/);
    assert.match(codexConfig, /model_auto_compact_token_limit_scope = "total"/);
    assert.ok(codex.args.includes("model_context_window=256000"));
    assert.ok(codex.args.includes("model_auto_compact_token_limit=204800"));
    assert.ok(codex.args.includes('model_auto_compact_token_limit_scope="total"'));
    assert.match(hermesConfig, /base_url: "http:\/\/127.0.0.1:4000\/v1"/);
    assert.equal(hermes.env.HERMES_HOME, path.join(launchHome, "hermes"));
    assert.match(hermesConfig, /context_length: 256000/);
    assert.match(hermesConfig, /threshold: 0\.8/);
    assert.match(hermesConfig, /threshold_tokens: 204800/);
    assert.match(dshPatch, /subroute\.settings\.yaml/);
    assert.equal(dsh.env.DSH_HOME, path.join(launchHome, "dsh"));
  } finally {
    await fs.rm(launchHome, { recursive: true, force: true });
  }
});

test("routes every launch through Subroute with a full-window compaction budget", async () => {
  const launchHome = await fs.mkdtemp(path.join(os.tmpdir(), "subroute-context-"));
  try {
    const gateway = "http://127.0.0.1:4005";
    const claude = await prepareLaunch("claude", gateway, launchHome);
    assert.equal(claude.env.ANTHROPIC_BASE_URL, gateway);
    assert.equal(claude.env.ANTHROPIC_MODEL, "current");
    assert.equal(claude.env.CLAUDE_CODE_SUBAGENT_MODEL, "current");
    assert.equal(claude.env.CLAUDE_CODE_MAX_CONTEXT_TOKENS, "256000");
    assert.equal(claude.env.CLAUDE_CODE_AUTO_COMPACT_WINDOW, "256000");
    assert.equal(claude.env.CLAUDE_AUTOCOMPACT_PCT_OVERRIDE, "80");
    assert.equal(claude.env.DISABLE_AUTO_COMPACT, "");
    assert.equal(claude.env.DISABLE_COMPACT, "");

    const opencode = await prepareLaunch("opencode", gateway, launchHome);
    const settings = JSON.parse(opencode.env.OPENCODE_CONFIG_CONTENT);
    const limits = settings.provider.subroute.models.current.limit;
    assert.equal(limits.context, 256000);
    assert.equal(limits.input - settings.compaction.reserved, 204800);
    assert.equal(settings.compaction.auto, true);
    assert.equal(settings.compaction.prune, false);
    assert.equal(opencode.env.OPENCODE_DISABLE_AUTOCOMPACT, "");

    const openclaw = await prepareLaunch("openclaw", gateway, launchHome);
    const claw = JSON.parse(await fs.readFile(openclaw.env.OPENCLAW_CONFIG_PATH, "utf8"));
    assert.equal(openclaw.env.OPENCLAW_STATE_DIR, path.join(launchHome, "openclaw"));
    assert.equal(claw.models.providers.subroute.baseUrl, `${gateway}/v1`);
    assert.equal(claw.agents.defaults.model.primary, "subroute/current");
    assert.equal(claw.models.providers.subroute.models[0].contextWindow, 256000);
    const clawSettings = JSON.parse(await fs.readFile(path.join(claw.agents.list[0].agentDir, "settings.json"), "utf8"));
    assert.equal(claw.models.providers.subroute.models[0].contextWindow - clawSettings.compaction.reserveTokens, 204800);
    assert.equal(clawSettings.compaction.enabled, true);
    assert.equal(claw.agents.defaults.compaction.enabled, true);

    const dsh = await prepareLaunch("dsh", gateway, launchHome);
    const dshSettings = await fs.readFile(path.join(dsh.env.DSH_HOME, "subroute.settings.yaml"), "utf8");
    assert.match(dshSettings, /contextWindow: 256000/);
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
