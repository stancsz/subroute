const native = window.subroute || null;
const agentFallback = [
  ["claude", "Claude Code", "Anthropic agentic coding CLI"], ["codex", "Codex", "OpenAI terminal coding CLI"],
  ["dsh", "DeepSeek Harness", "DeepSeek coding agent"], ["hermes", "Hermes", "Nous Research terminal agent"],
  ["openclaw", "OpenClaw", "Open-source coding agent CLI"], ["opencode", "OpenCode", "Open-source terminal coding agent"],
].map(([id, name, protocol]) => ({ id, name, protocol, installed: false, ready: false }));
const cards = document.querySelector("#cards"), cardTemplate = document.querySelector("#card"), updated = document.querySelector("#updated");
const routeModel = document.querySelector("#route-model"), routeMode = document.querySelector("#route-mode"), routeAdvisor = document.querySelector("#route-advisor");
const routeProvider = document.querySelector("#route-provider"), advisorProvider = document.querySelector("#advisor-provider");
const routeEffort = document.querySelector("#route-effort"), advisorEffort = document.querySelector("#route-advisor-effort");
const routeStatus = document.querySelector("#route-status"), routeKey = document.querySelector("#route-key");
let workingDirectory = null, routingReady = false;
let routingModels = [], advisorModels = [];

async function request(path, options) {
  const response = await fetch(path, options);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || `Gateway request failed (${response.status})`);
  return payload;
}

function option(value, label) { const node = document.createElement("option"); node.value = value; node.textContent = label; return node; }

function setRoutingDisabled(value) {
  for (const select of [routeProvider, advisorProvider, routeMode]) select.disabled = value;
  routeModel.disabled = value || !routeProvider.value;
  routeAdvisor.disabled = value || !advisorProvider.value;
  routeEffort.disabled = value || !routeModel.value || routeMode.value === "off" || routeEffort.options.length < 2;
  advisorEffort.disabled = value || !routeAdvisor.value || advisorEffort.options.length < 2;
}

function renderProviderMenu(select, choices, selected, advisor = false) {
  const visible = choices.filter(item => item.configured || item.model_id === selected);
  const providers = [...new Set(visible.map(item => item.provider))].sort();
  select.replaceChildren(...(advisor ? [option("", "No advisor")] : []), ...providers.map(provider => option(provider, provider)));
  if (!providers.length && !advisor) select.append(option("", "No providers configured"));
  select.value = choices.find(item => item.model_id === selected)?.provider || "";
}

function renderModelMenu(select, choices, provider, selected) {
  const visible = choices.filter(item => item.provider === provider && (item.configured || item.model_id === selected));
  select.replaceChildren();
  if (!selected) select.append(option("", provider ? "Choose a model…" : "No advisor selected"));
  for (const access of [...new Set(visible.map(item => item.access))]) {
    const group = document.createElement("optgroup"); group.label = access;
    for (const item of visible.filter(item => item.access === access)) {
      const entry = option(item.model_id, `${item.display_name}${item.configured ? "" : " (setup required)"}`);
      entry.disabled = !item.configured; group.append(entry);
    }
    select.append(group);
  }
  select.value = selected || "";
}

function renderEfforts(select, choices, model, selected, helpId, advisor = false) {
  const choice = choices.find(item => item.model_id === model);
  const efforts = choice?.configured ? choice.reasoning_efforts || [] : [];
  const labels = { minimal: "Minimal", low: "Low", medium: "Medium", high: "High", xhigh: "Extra high", max: "Maximum", ultra: "Ultra" };
  const fallback = !model ? "Choose a model first" : !choice?.configured ? "Provider setup required" : efforts.length ? "Default (client or provider)" : "Provider managed";
  select.replaceChildren(option("", fallback), ...efforts.map(effort => option(effort, labels[effort] || effort)));
  select.value = efforts.includes(selected) ? selected : "";
  let help = !model ? (advisor && !advisorProvider.value ? "No advisor will be consulted." : "Only models from configured connections are shown.") : !choice?.configured ? "This saved route needs provider configuration before it can be used." : !efforts.length ? (choice.reasoning_note || "This route has no verified reasoning effort control.") : advisor ? `${choice.access}. Reasoning is independent of the target.` : `${choice.access}. Lower effort favors speed; higher effort allows more reasoning.`;
  if (efforts.length && choice.reasoning_note) help += ` ${choice.reasoning_note}`;
  if (model?.startsWith("gemini-subscription") && !advisor) help += " Text only; no streaming or tools.";
  if (!advisor && routeMode.value === "off") help = "Routing is off. The client chooses the model and reasoning.";
  document.querySelector(helpId).textContent = help;
}

async function renderRouting() {
  routingReady = false;
  try {
    const routing = await request("/api/routing-options");
    if (!routing.models?.every(item => item.provider && typeof item.configured === "boolean")) {
      routeProvider.replaceChildren(option("", "Gateway update required"));
      advisorProvider.replaceChildren(option("", "Gateway update required"));
      routeModel.replaceChildren(option("", "Refresh after gateway restart"));
      routeAdvisor.replaceChildren(option("", "Refresh after gateway restart"));
      throw new Error("The page and gateway versions differ. Refresh after the gateway restarts.");
    }
    routingModels = routing.models; advisorModels = routing.advisor_models;
    renderProviderMenu(routeProvider, routingModels, routing.state.active_model);
    renderProviderMenu(advisorProvider, advisorModels, routing.state.advisor_model, true);
    renderModelMenu(routeModel, routingModels, routeProvider.value, routing.state.active_model);
    renderModelMenu(routeAdvisor, advisorModels, advisorProvider.value, routing.state.advisor_model);
    routeMode.value = routing.state.mode;
    renderEfforts(routeEffort, routingModels, routeModel.value, routing.state.reasoning_effort, "#route-effort-help");
    renderEfforts(advisorEffort, advisorModels, routeAdvisor.value, routing.state.advisor_reasoning_effort, "#route-advisor-effort-help", true);
    routeStatus.textContent = `${routing.client_api_key_required ? "Client API key required" : "No client API key required"} · policy v${routing.state.policy_version}`;
    routeKey.textContent = routing.client_api_key_required ? "CLIENT API KEY REQUIRED" : "LOCAL CLIENT KEY BYPASS";
    setRoutingDisabled(false); routingReady = true;
  } catch (error) { setRoutingDisabled(true); routeStatus.textContent = error.message; routeKey.textContent = "GATEWAY UNAVAILABLE"; }
}

async function saveActiveRouting() {
  if (!routingReady) return;
  setRoutingDisabled(true); routeStatus.textContent = "Saving routing…";
  try {
    const state = await request("/api/active-model", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ model: routeModel.value, mode: routeMode.value, reasoning_effort: routeEffort.value || null }) });
    routeStatus.textContent = `Routing saved · policy v${state.policy_version}`; setRoutingDisabled(false);
  } catch (error) { await renderRouting(); routeStatus.textContent = `Not saved: ${error.message}`; }
}

async function saveAdvisorRouting() {
  if (!routingReady) return;
  setRoutingDisabled(true); routeStatus.textContent = "Saving advisor…";
  try {
    const state = await request("/api/advisor-model", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ advisor_model: routeAdvisor.value || null, reasoning_effort: advisorEffort.value || null }) });
    routeStatus.textContent = `Advisor ${state.advisor_model ? "saved" : "disabled"} · policy v${state.policy_version}`; setRoutingDisabled(false);
  } catch (error) { await renderRouting(); routeStatus.textContent = `Not saved: ${error.message}`; }
}

function renderSources(sources, usageMap) {
  cards.replaceChildren(...sources.sort((a, b) => a.name.localeCompare(b.name)).map(source => {
    const node = cardTemplate.content.cloneNode(true), card = node.querySelector(".card"), usage = usageMap[source.id] || { state: "unavailable", detail: "Provider quota data unavailable" };
    card.classList.add(source.accent || "blue"); node.querySelector("h3").textContent = source.name; node.querySelector(".kind").textContent = source.kind; node.querySelector(".models").textContent = source.models;
    const connection = source.configured ? "Configured" : source.available ? "Credential required" : "Not added to gateway", line = node.querySelector(".usage"), footer = node.querySelector("footer"), bar = node.querySelector("i");
    if (usage.state === "ready") { const percent = Math.min(100, usage.used / usage.limit * 100), remaining = usage.remaining ?? Math.max(0, usage.limit - usage.used), usd = usage.detail === "USD credits"; line.textContent = `${connection} · ${usd ? `$${remaining.toFixed(2)}` : `${Math.round(remaining)}%`} remaining`; footer.textContent = `${usd ? `$${usage.used.toFixed(2)} used` : `${Math.round(percent)}% used`} · ${usage.detail}`; bar.style.width = `${percent}%`; }
    else if (usage.state === "connected") { line.textContent = `${connection} · authenticated`; footer.textContent = usage.detail; bar.style.width = "100%"; }
    else if (usage.state === "sign_in_required") { line.textContent = "Docker bridge ready · sign-in required"; footer.textContent = usage.detail; bar.style.width = "0%"; }
    else { line.textContent = `${connection} · usage unavailable`; footer.textContent = usage.detail; bar.style.width = "0%"; }
    return node;
  }));
}

async function chooseDirectory() { if (!native) return false; const selected = await native.chooseDirectory(); if (!selected) return false; workingDirectory = selected; const field = document.querySelector("#working-directory"); field.textContent = selected; field.title = selected; document.querySelector("#launch-status").textContent = `Selected: ${selected}`; return true; }

async function renderAgents() {
  const agents = native ? await native.listAgents() : agentFallback, area = document.querySelector("#agents"), template = document.querySelector("#agent");
  area.replaceChildren(...agents.sort((a, b) => a.name.localeCompare(b.name)).map(agent => { const node = template.content.cloneNode(true), button = node.querySelector("button"), state = node.querySelector(".agent-state"), ready = agent.installed && agent.ready; node.querySelector("b").textContent = agent.name; node.querySelector("small").textContent = agent.protocol; state.classList.add(native ? (ready ? "installed" : "missing") : "desktop-only"); button.textContent = native ? (ready ? "Launch" : agent.installed ? "CLI check failed" : "Install") : "Desktop only"; button.disabled = !native || (agent.installed && !ready); if (native) button.addEventListener("click", async () => { try { if (!agent.installed) { button.disabled = true; button.textContent = "Opening installer"; const result = await native.installAgent({ id: agent.id }); document.querySelector("#launch-status").textContent = `${result.detail} opened for ${result.name}. Refresh after installation finishes.`; return; } if (!workingDirectory && !await chooseDirectory()) return; const result = await native.launchAgent({ id: agent.id, directory: workingDirectory }); document.querySelector("#launch-status").textContent = `Opened ${result.name} in ${workingDirectory}.`; } catch (error) { document.querySelector("#launch-status").textContent = error.message; button.disabled = false; } }); return node; }));
}

async function load(refreshUsage = false) {
  const button = document.querySelector("#refresh"); button.disabled = true;
  const controls = Promise.all([renderRouting(), renderAgents()]);
  try { const [sourceData, usage] = await Promise.all([request("/api/source-status"), request(`/api/provider-usage${refreshUsage ? "?refresh=true" : ""}`)]); renderSources(sourceData.sources || [], usage.sources || {}); updated.textContent = usage.updated_at ? `Updated ${new Date(usage.updated_at).toLocaleTimeString()}` : "Usage not refreshed"; }
  catch (error) { updated.textContent = error.message; }
  finally { await controls; button.disabled = false; }
}
document.querySelector("#gateway").textContent = location.origin;
document.querySelector("#surface").textContent = native ? "DESKTOP CONTROL" : "WEB CONTROL";
document.querySelector("#launcher-capability").textContent = native ? "NATIVE LAUNCH ENABLED" : "OPEN DESKTOP TO LAUNCH";
document.querySelector("#choose-directory").disabled = !native;
document.querySelector("#refresh").addEventListener("click", () => load(true)); document.querySelector("#choose-directory").addEventListener("click", chooseDirectory);
routeProvider.addEventListener("change", () => {
  renderModelMenu(routeModel, routingModels, routeProvider.value, null);
  renderEfforts(routeEffort, routingModels, "", null, "#route-effort-help");
  setRoutingDisabled(false); routeStatus.textContent = "Choose a target model to save this provider.";
});
advisorProvider.addEventListener("change", () => {
  renderModelMenu(routeAdvisor, advisorModels, advisorProvider.value, null);
  renderEfforts(advisorEffort, advisorModels, "", null, "#route-advisor-effort-help", true);
  setRoutingDisabled(false);
  if (!advisorProvider.value) saveAdvisorRouting();
  else routeStatus.textContent = "Choose an advisor model to save this provider.";
});
routeModel.addEventListener("change", () => { renderEfforts(routeEffort, routingModels, routeModel.value, null, "#route-effort-help"); saveActiveRouting(); });
routeMode.addEventListener("change", saveActiveRouting);
routeEffort.addEventListener("change", saveActiveRouting);
routeAdvisor.addEventListener("change", () => { renderEfforts(advisorEffort, advisorModels, routeAdvisor.value, null, "#route-advisor-effort-help", true); saveAdvisorRouting(); });
advisorEffort.addEventListener("change", saveAdvisorRouting);
load(false);
