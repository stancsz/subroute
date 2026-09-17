"use strict";
const { execFile, spawn } = require("node:child_process");
const { promisify } = require("node:util");
const execFileAsync = promisify(execFile);

const AGENTS = {
  claude: { name: "Claude Code", command: "claude", args: ["--model", "current"], protocol: "Anthropic" },
  codex: { name: "Codex", command: "codex", args: ["-m", "current"], protocol: "Responses" },
  opencode: { name: "OpenCode", command: "opencode", args: [], protocol: "OpenAI compatible" },
  openclaw: { name: "OpenClaw", command: "openclaw", args: ["chat"], protocol: "OpenAI compatible" },
  hermes: { name: "Hermes", command: "hermes", args: ["chat"], protocol: "OpenAI compatible" },
  dsh: { name: "DeepSeek Harness", command: "dsh", args: ["web"], protocol: "OpenAI compatible" },
};

function sessionEnv(gatewayUrl) {
  const origin = gatewayUrl.replace(/\/$/, "");
  const openai = `${origin}/v1`;
  return {
    ...process.env,
    OPENAI_BASE_URL: openai, OPENAI_API_BASE: openai, OPENAI_API_KEY: "subroute-local",
    ANTHROPIC_BASE_URL: origin, ANTHROPIC_AUTH_TOKEN: "subroute-local", ANTHROPIC_API_KEY: "",
    ANTHROPIC_MODEL: "current", ANTHROPIC_DEFAULT_OPUS_MODEL: "current", ANTHROPIC_DEFAULT_SONNET_MODEL: "current", ANTHROPIC_DEFAULT_HAIKU_MODEL: "current",
    DEEPSEEK_BASE_URL: openai, DEEPSEEK_API_KEY: "subroute-local",
    OPENCODE_CONFIG_CONTENT: JSON.stringify({ provider: { subroute: { npm: "@ai-sdk/openai-compatible", name: "Subroute", options: { baseURL: openai }, models: { current: { name: "current" } } } }, model: "subroute/current" }),
  };
}

async function availability() {
  return Promise.all(Object.entries(AGENTS).map(async ([id, agent]) => {
    try { const { stdout } = await execFileAsync("where.exe", [agent.command], { windowsHide: true }); return { id, ...agent, installed: true, path: stdout.trim().split(/\r?\n/)[0] }; }
    catch { return { id, ...agent, installed: false }; }
  }));
}

function launch(id, cwd, gatewayUrl) {
  const agent = AGENTS[id];
  if (!agent) throw new Error("Unknown agent");
  if (!cwd) throw new Error("Choose a working directory first");
  const quoted = value => `"${String(value).replaceAll('"', '\\"')}"`;
  const command = [quoted(agent.command), ...agent.args.map(quoted)].join(" ");
  const child = spawn("cmd.exe", ["/d", "/s", "/c", `start "Subroute · ${agent.name}" /D ${quoted(cwd)} ${command}`], { cwd, env: sessionEnv(gatewayUrl), detached: true, stdio: "ignore", windowsHide: false });
  child.unref();
  return { name: agent.name, command };
}

module.exports = { availability, launch };
