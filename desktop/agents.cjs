"use strict";
const fs = require("node:fs/promises");
const path = require("node:path");
const { execFile, spawn } = require("node:child_process");
const { promisify } = require("node:util");
const execFileAsync = promisify(execFile);

const MODEL = "current";
const PROFILE = "subroute";
const AGENTS = {
  claude: { name: "Claude Code", command: "claude", protocol: "Anthropic" },
  codex: { name: "Codex", command: "codex", protocol: "Responses" },
  opencode: { name: "OpenCode", command: "opencode", protocol: "OpenAI compatible" },
  openclaw: { name: "OpenClaw", command: "openclaw", protocol: "OpenAI compatible" },
  hermes: { name: "Hermes", command: "hermes", protocol: "OpenAI compatible" },
  dsh: { name: "DeepSeek Harness", command: "dsh", protocol: "OpenAI compatible" },
};
const INSTALLERS = {
  claude: {
    command: "irm https://claude.ai/install.ps1 | iex",
    detail: "Official Claude Code Windows installer",
  },
  codex: {
    command: "npm install --global @openai/codex",
    detail: "Official Codex CLI npm installer",
  },
  opencode: {
    command: "npm install --global @opencode/cli",
    detail: "Official OpenCode CLI npm installer",
  },
  // The documented installer is told not to open its setup wizard or gateway.
  openclaw: {
    command: "& ([scriptblock]::Create((iwr -useb https://openclaw.ai/install.ps1))) -NoOnboard",
    detail: "Official OpenClaw Windows installer",
  },
  hermes: {
    command: "& ([scriptblock]::Create((irm https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.ps1))) -SkipSetup",
    detail: "Official Hermes Agent Windows installer",
  },
  dsh: {
    command: "npm install --global @deepseek-ai/dsh",
    detail: "DeepSeek Harness npm installer",
  },
};

function gatewayEndpoints(gatewayUrl) {
  const origin = String(gatewayUrl || "").replace(/\/$/, "");
  if (!/^https?:\/\//.test(origin)) throw new Error("Gateway URL must start with http:// or https://");
  return { origin, openai: `${origin}/v1` };
}

function sessionEnv(gatewayUrl) {
  const { origin, openai } = gatewayEndpoints(gatewayUrl);
  return {
    ...process.env,
    OPENAI_BASE_URL: openai,
    OPENAI_API_BASE: openai,
    OPENAI_API_KEY: "subroute-local",
    ANTHROPIC_BASE_URL: origin,
    ANTHROPIC_AUTH_TOKEN: "subroute-local",
    ANTHROPIC_API_KEY: "",
    ANTHROPIC_MODEL: MODEL,
    ANTHROPIC_DEFAULT_OPUS_MODEL: MODEL,
    ANTHROPIC_DEFAULT_SONNET_MODEL: MODEL,
    ANTHROPIC_DEFAULT_HAIKU_MODEL: MODEL,
    CLAUDE_CODE_SUBAGENT_MODEL: MODEL,
    DEEPSEEK_BASE_URL: openai,
    DEEPSEEK_API_KEY: "subroute-local",
    DSH_TELEMETRY_DISABLED: "1",
  };
}

function desktopLaunchHome() {
  const appData = process.env.APPDATA || path.join(process.env.USERPROFILE || process.cwd(), "AppData", "Roaming");
  return path.join(appData, "subroute-desktop", "launch");
}

async function writeLaunchFile(filePath, contents) {
  await fs.mkdir(path.dirname(filePath), { recursive: true });
  await fs.writeFile(filePath, contents, "utf8");
}

function catalog() {
  return JSON.stringify({ models: [{
    slug: MODEL, display_name: MODEL, description: "Subroute active model", context_window: 128000,
    max_context_window: 128000, effective_context_window_percent: 100, shell_type: "default", visibility: "list",
    supported_in_api: true, priority: 0, additional_speed_tiers: [], service_tiers: [],
    truncation_policy: { mode: "bytes", limit: 10000 }, input_modalities: ["text", "image"], base_instructions: "",
    default_reasoning_summary: "none", supported_reasoning_levels: [], supports_reasoning_summaries: false,
    experimental_supported_tools: [], supports_search_tool: false, web_search_tool_type: "text",
    multi_agent_version: "v2",
    supports_image_detail_original: false, support_verbosity: true, default_verbosity: "low",
    supports_parallel_tool_calls: false, use_responses_lite: false, model_messages: null, upgrade: null,
  }] }, null, 2);
}

async function prepareLaunch(id, gatewayUrl, launchHome = desktopLaunchHome()) {
  const agent = AGENTS[id];
  if (!agent) throw new Error("Unknown agent");
  const { openai } = gatewayEndpoints(gatewayUrl);
  const env = sessionEnv(gatewayUrl);
  let args = [];

  if (id === "claude") {
    args = ["--model", MODEL];
  } else if (id === "codex") {
    const codexHome = path.join(launchHome, "codex");
    const catalogPath = path.join(codexHome, "models.json");
    await writeLaunchFile(catalogPath, catalog());
    await writeLaunchFile(path.join(codexHome, `${PROFILE}.config.toml`), [
      `model = ${JSON.stringify(MODEL)}`,
      `model_provider = ${JSON.stringify(PROFILE)}`,
      `model_catalog_json = ${JSON.stringify(catalogPath)}`,
      "",
      `[model_providers.${PROFILE}]`,
      'name = "Subroute"',
      `base_url = ${JSON.stringify(openai)}`,
      'wire_api = "responses"',
      'env_key = "SUBROUTE_API_KEY"',
      "",
    ].join("\n"));
    env.CODEX_HOME = codexHome;
    env.SUBROUTE_API_KEY = "subroute-local";
    // Electron may inherit TERM=dumb. Codex's interactive TUI requires a capable terminal.
    env.TERM = "xterm-256color";
    args = ["--profile", PROFILE, "-c", `model_catalog_json=${JSON.stringify(catalogPath)}`, "--model", MODEL];
  } else if (id === "opencode") {
    env.OPENCODE_CONFIG_CONTENT = JSON.stringify({
      $schema: "https://opencode.ai/config.json",
      provider: { subroute: { npm: "@ai-sdk/openai-compatible", name: "Subroute", options: { baseURL: openai }, models: { [MODEL]: { name: MODEL, limit: { context: 128000, output: 32768 }, modalities: { input: ["text", "image"], output: ["text"] } } } } },
      model: `subroute/${MODEL}`,
    });
    args = ["--model", `subroute/${MODEL}`];
  } else if (id === "hermes") {
    const hermesHome = path.join(launchHome, "hermes");
    await writeLaunchFile(path.join(hermesHome, "config.yaml"), [
      "model:", `  default: ${MODEL}`, "  provider: custom", `  base_url: ${JSON.stringify(openai)}`,
      "  api_key: subroute-local", "  context_length: 128000", "",
    ].join("\n"));
    env.HERMES_HOME = hermesHome;
    args = ["chat", "--model", MODEL, "--provider", "custom"];
  } else if (id === "dsh") {
    const dshHome = path.join(launchHome, "dsh");
    const settingsPath = path.join(dshHome, "subroute.settings.yaml");
    const patchPath = path.join(dshHome, "subroute.patch.yaml");
    await writeLaunchFile(settingsPath, [
      "llm-deepseek:", `  baseURL: ${JSON.stringify(openai)}`, "  models:", `    - id: ${MODEL}`,
      "      name: Subroute active model", "      inputModalities: [text, image]", "      contextWindow: 128000", "      maxTokens: 32768",
      "agent-default-model:", "  provider: deepseek-official", `  model: ${MODEL}`, "",
    ].join("\n"));
    await writeLaunchFile(patchPath, ["- id: settings", "  config:", `    path: ${JSON.stringify(settingsPath)}`, ""].join("\n"));
    env.DSH_HOME = dshHome;
    args = ["--profile", "web", "--patch", patchPath];
  } else if (id === "openclaw") {
    args = ["chat"];
  }
  return { ...agent, args, env, launchHome };
}

async function findCommand(command) {
  try {
    const { stdout } = await execFileAsync("where.exe", [`${command}.cmd`], { windowsHide: true });
    return stdout.trim().split(/\r?\n/)[0];
  } catch {
    try {
      const { stdout } = await execFileAsync("where.exe", [command], { windowsHide: true });
      return stdout.trim().split(/\r?\n/)[0];
    } catch {
      // Native installers can update the user PATH after Electron has already started.
      // Check their documented locations directly so Refresh detects a just-finished install.
      const home = process.env.USERPROFILE || "";
      const localAppData = process.env.LOCALAPPDATA || path.join(home, "AppData", "Local");
      const fallbacks = {
        claude: [path.join(home, ".local", "bin", "claude.exe"), path.join(home, ".local", "bin", "claude.cmd")],
        codex: [path.join(home, ".local", "bin", "codex.exe")],
        openclaw: [path.join(home, ".local", "bin", "openclaw.cmd")],
        hermes: [path.join(localAppData, "hermes", "bin", "hermes.exe")],
        dsh: [path.join(process.env.APPDATA || "", "npm", "dsh.cmd")],
        opencode: [path.join(process.env.APPDATA || "", "npm", "opencode.cmd")],
      };
      for (const candidate of fallbacks[command] || []) {
        if (await fs.stat(candidate).then(file => file.isFile()).catch(() => false)) return candidate;
      }
      return null;
    }
  }
}

async function availability() {
  return Promise.all(Object.entries(AGENTS).map(async ([id, agent]) => {
    const commandPath = await findCommand(agent.command);
    if (!commandPath) return { id, ...agent, installed: false };
    try {
      // Windows batch shims such as npm's claude.cmd cannot be run with execFile directly.
      // cmd's CALL keeps the check in the same process and works for both shims and executables.
      await execFileAsync("cmd.exe", ["/d", "/c", "call", commandPath, "--version"], { windowsHide: true, timeout: 10000 });
      return { id, ...agent, installed: true, ready: true, path: commandPath };
    } catch {
      return { id, ...agent, installed: true, ready: false, path: commandPath, detail: "CLI did not pass its local version check" };
    }
  }));
}

function quotePowerShell(value) { return `'${String(value).replaceAll("'", "''")}'`; }

function powerShellInvocation(commandPath, args) {
  return `& ${[commandPath, ...args].map(quotePowerShell).join(" ")}`;
}

function openTerminal(cwd, env, command) {
  return spawn("cmd.exe", ["/d", "/c", "start", "", "/D", cwd, "powershell.exe", "-NoLogo", "-NoExit", "-Command", command], {
    cwd, env, stdio: "ignore", windowsHide: false,
  });
}

async function launch(id, cwd, gatewayUrl) {
  if (!cwd) throw new Error("Choose a working directory first");
  const directory = await fs.stat(cwd).catch(() => null);
  if (!directory?.isDirectory()) throw new Error("The selected working directory is no longer available.");
  const agent = AGENTS[id];
  if (!agent) throw new Error("Unknown agent");
  const commandPath = await findCommand(agent.command);
  if (!commandPath) throw new Error(`${agent.name} is not installed. Install its CLI first, then refresh this app.`);
  const prepared = await prepareLaunch(id, gatewayUrl);
  const invocation = powerShellInvocation(commandPath, prepared.args);
  // `start` owns the visible terminal. Its first quoted token is always a window title,
  // so pass an explicit empty title before the PowerShell command. This avoids treating
  // a decorative title as the executable, which was the cause of the failed launches.
  openTerminal(cwd, prepared.env, invocation);
  return { name: agent.name, command: `${agent.command} ${prepared.args.join(" ")}`.trim() };
}

function install(id, cwd = process.cwd()) {
  const agent = AGENTS[id];
  const installer = INSTALLERS[id];
  if (!agent || !installer) throw new Error(`No supported installer is available for ${agent?.name || "this agent"}.`);
  openTerminal(cwd, process.env, installer.command);
  return { name: agent.name, detail: installer.detail };
}

module.exports = { AGENTS, INSTALLERS, availability, desktopLaunchHome, gatewayEndpoints, prepareLaunch, powerShellInvocation, launch, install, sessionEnv };
