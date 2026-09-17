const native = window.subroute || null;
const agentFallback = [
  ["claude", "Claude Code", "Anthropic agentic coding CLI"], ["codex", "Codex", "OpenAI terminal coding CLI"],
  ["dsh", "DeepSeek Harness", "DeepSeek coding agent"], ["hermes", "Hermes", "Nous Research terminal agent"],
  ["openclaw", "OpenClaw", "Open-source coding agent CLI"], ["opencode", "OpenCode", "Open-source terminal coding agent"],
].map(([id, name, protocol]) => ({ id, name, protocol, installed: false, ready: false }));
const cards = document.querySelector("#cards"), cardTemplate = document.querySelector("#card"), updated = document.querySelector("#updated");
const routeModel = document.querySelector("#route-model"), routeMode = document.querySelector("#route-mode"), routeAdvisor = document.querySelector("#route-advisor");
const routeStatus = document.querySelector("#route-status"), routeKey = document.querySelector("#route-key");
let workingDirectory = null, routingReady = false;

async function request(path, options) {
  const response = await fetch(path, options);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || `Gateway request failed (${response.status})`);
  return payload;
}

function option(value, label) { const node = document.createElement("option"); node.value = value; node.textContent = label; return node; }
function setRoutingDisabled(value) { for (const select of [routeModel, routeMode, routeAdvisor]) select.disabled = value; }
async function renderRouting() {
  routingReady = false;
  try {
    const routing = await request("/api/routing-options");
    routeModel.replaceChildren(...routing.models.map(item => option(item.model_id, item.display_name || item.model_id)));
    routeAdvisor.replaceChildren(option("", "No advisor"), ...routing.advisor_models.map(item => option(item.model_id, item.display_name || item.model_id)));
    routeModel.value = routing.state.active_model; routeMode.value = routing.state.mode; routeAdvisor.value = routing.state.advisor_model || "";
    routeStatus.textContent = `${routing.client_api_key_required ? "Client API key required" : "No client API key required"} · policy v${routing.state.policy_version}`;
    routeKey.textContent = routing.client_api_key_required ? "CLIENT API KEY REQUIRED" : "LOCAL CLIENT KEY BYPASS";
    setRoutingDisabled(false); routingReady = true;
  } catch (error) { setRoutingDisabled(true); routeStatus.textContent = error.message; routeKey.textContent = "GATEWAY UNAVAILABLE"; }
}
async function saveActiveRouting() { if (!routingReady) return; setRoutingDisabled(true); try { const state = await request("/api/active-model", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ model: routeModel.value, mode: routeMode.value }) }); routeStatus.textContent = `Routing saved · policy v${state.policy_version}`; setRoutingDisabled(false); } catch (error) { routeStatus.textContent = error.message; await renderRouting(); } }
async function saveAdvisorRouting() { if (!routingReady) return; setRoutingDisabled(true); try { const state = await request("/api/advisor-model", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ advisor_model: routeAdvisor.value || null }) }); routeStatus.textContent = `Advisor ${state.advisor_model ? "saved" : "disabled"} · policy v${state.policy_version}`; setRoutingDisabled(false); } catch (error) { routeStatus.textContent = error.message; await renderRouting(); } }

function renderSources(sources, usageMap) {
  cards.replaceChildren(...sources.sort((a, b) => a.name.localeCompare(b.name)).map(source => {
    const node = cardTemplate.content.cloneNode(true), card = node.querySelector(".card"), usage = usageMap[source.id] || { state: "unavailable", detail: "Provider quota data unavailable" };
    card.classList.add(source.accent || "blue"); node.querySelector("h3").textContent = source.name; node.querySelector(".kind").textContent = source.kind; node.querySelector(".models").textContent = source.models;
    const connection = source.configured ? "Configured" : source.available ? "Credential required" : "Not added to gateway", line = node.querySelector(".usage"), footer = node.querySelector("footer"), bar = node.querySelector("i");
    if (usage.state === "ready") { const percent = Math.min(100, usage.used / usage.limit * 100), remaining = usage.remaining ?? Math.max(0, usage.limit - usage.used), usd = usage.detail === "USD credits"; line.textContent = `${connection} · ${usd ? `$${remaining.toFixed(2)}` : `${Math.round(remaining)}%`} remaining`; footer.textContent = `${usd ? `$${usage.used.toFixed(2)} used` : `${Math.round(percent)}% used`} · ${usage.detail}`; bar.style.width = `${percent}%`; }
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
  try { const [sourceData, usage] = await Promise.all([request("/api/source-status"), request(`/api/provider-usage${refreshUsage ? "?refresh=true" : ""}`)]); renderSources(sourceData.sources || [], usage.sources || {}); updated.textContent = usage.updated_at ? `Updated ${new Date(usage.updated_at).toLocaleTimeString()}` : "Usage not refreshed"; await Promise.all([renderRouting(), renderAgents()]); }
  catch (error) { updated.textContent = error.message; }
  finally { button.disabled = false; }
}

document.querySelector("#gateway").textContent = location.origin;
document.querySelector("#surface").textContent = native ? "DESKTOP CONTROL" : "WEB CONTROL";
document.querySelector("#launcher-capability").textContent = native ? "NATIVE LAUNCH ENABLED" : "OPEN DESKTOP TO LAUNCH";
document.querySelector("#choose-directory").disabled = !native;
document.querySelector("#refresh").addEventListener("click", () => load(true)); document.querySelector("#choose-directory").addEventListener("click", chooseDirectory);
routeModel.addEventListener("change", saveActiveRouting); routeMode.addEventListener("change", saveActiveRouting); routeAdvisor.addEventListener("change", saveAdvisorRouting);
load(false);
