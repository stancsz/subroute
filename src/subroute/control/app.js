const native = window.subroute || null;
const agentFallback = [
  ["claude", "Claude Code", "Anthropic agentic coding CLI"], ["codex", "Codex", "OpenAI terminal coding CLI"],
  ["dsh", "DeepSeek Harness", "DeepSeek coding agent"], ["hermes", "Hermes", "Nous Research terminal agent"],
  ["openclaw", "OpenClaw", "Open-source coding agent CLI"], ["opencode", "OpenCode", "Open-source terminal coding agent"],
].map(([id, name, protocol]) => ({ id, name, protocol, installed: false, ready: false }));
const cards = document.querySelector("#cards"), cardTemplate = document.querySelector("#card"), updated = document.querySelector("#updated");
const moreCards = document.querySelector("#more-cards"), connectionCount = document.querySelector("#connection-count"), otherCount = document.querySelector("#other-count");
const routeModel = document.querySelector("#route-model"), routeMode = document.querySelector("#route-mode"), routeAdvisor = document.querySelector("#route-advisor");
const routeProvider = document.querySelector("#route-provider"), advisorProvider = document.querySelector("#advisor-provider");
const routeEffort = document.querySelector("#route-effort"), advisorEffort = document.querySelector("#route-advisor-effort");
const routeFast = document.querySelector("#route-fast"), routeFastField = document.querySelector("#route-fast-field"), routeFastHelp = document.querySelector("#route-fast-help");
const advisorFrequency = document.querySelector("#route-advisor-frequency"), advisorFrequencyValue = document.querySelector("#route-advisor-frequency-value");
const advisorFrequencyStats = document.querySelector("#route-advisor-frequency-stats");
const advisorAnalyticsWindow = document.querySelector("#route-advisor-analytics-window");
const advisorAnalyticsRatios = document.querySelector("#route-advisor-analytics-ratios"), advisorAnalyticsFailures = document.querySelector("#route-advisor-analytics-failures");
const advisorAnalyticsCoverage = document.querySelector("#route-advisor-analytics-coverage"), advisorAnalyticsScope = document.querySelector("#route-advisor-analytics-scope");
const frequencyPresets = ["conservative", "balanced", "frequent"], frequencyNames = ["Less often", "Balanced", "More often"], frequencyRates = [30, 50, 70];
const routeStatus = document.querySelector("#route-status"), routeKey = document.querySelector("#route-key");
const policySummary = document.querySelector("#policy-summary");
new MutationObserver(() => {
  const status = routeStatus.textContent;
  const failed = status.startsWith("Not saved:");
  policySummary.dataset.state = failed ? "error" : !routingReady ? "offline" : "ready";
  policySummary.textContent = failed ? "Not saved · previous policy remains active" : !routingReady ? "Policy unavailable · editing disabled" : (status.includes("Saving") || status.startsWith("Choose")) ? status : `Saved policy ${status.match(/v\d+/)?.[0] || "active"}`;
  document.querySelector("#routing-description").textContent = !routingReady ? "The policy cannot be edited until the gateway reconnects." : "Changes save automatically and apply to new requests.";
}).observe(routeStatus, {childList:true, subtree:true, characterData:true});
let workingDirectory = null, routingReady = false;
let routingModels = [], advisorModels = [];
let analyticsPending = false;

async function request(path, options) {
  let response;
  try { response = await fetch(path, options); }
  catch (error) { throw error; }
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || `Gateway request failed (${response.status})`);
  return payload;
}

function setGatewayStatus(online) {
  const pill = document.querySelector(".connection-pill");
  document.querySelector("#gateway-status").textContent = online ? "Gateway online" : "Gateway unavailable";
  pill.classList.toggle("offline", !online);
}

function option(value, label) { const node = document.createElement("option"); node.value = value; node.textContent = label; return node; }

function setRoutingDisabled(value) {
  for (const select of [routeProvider, advisorProvider, routeMode]) select.disabled = value;
  routeModel.disabled = value || !routeProvider.value;
  routeAdvisor.disabled = value || !advisorProvider.value;
  routeEffort.disabled = value || !routeModel.value || routeMode.value === "off" || routeEffort.options.length < 2;
  routeFast.disabled = value || routeFastField.hidden || routeMode.value === "off";
  advisorEffort.disabled = value || !routeAdvisor.value || advisorEffort.options.length < 2;
  advisorFrequency.disabled = value || !routeAdvisor.value;
  renderAdvisorFrequency(value);
}

function renderAdvisorFrequency(busy = false) {
  const index = Number(advisorFrequency.value);
  advisorFrequencyValue.textContent = `~${frequencyRates[index]}%`;
  advisorFrequency.setAttribute("aria-valuetext", `${frequencyNames[index]}, about ${frequencyRates[index]} percent`);
  document.querySelectorAll(".frequency-stops span").forEach((node, i) => node.classList.toggle("selected", i === index));
  document.querySelector("#route-advisor-frequency-state").textContent = busy ? "Frequency control is unavailable while policy loads or saves." : !routeAdvisor.value ? "Choose an advisor to enable selection." : "Selected requests receive the chosen advisor plus Claude Opus 5.5 Low via Antigravity (max 32,000 input tokens) before the target responds. Oversized context is compacted with Sift, then MiniMax or MiMo.";
}

function clearAdvisorAnalytics(message) {
  advisorFrequencyStats.textContent = message;
  advisorAnalyticsRatios.textContent = "";
  advisorAnalyticsFailures.hidden = true;
  advisorAnalyticsCoverage.textContent = "";
}

async function refreshAdvisorAnalytics() {
  if (analyticsPending || document.hidden) return;
  const minutes = Number(advisorAnalyticsWindow.value);
  const windowLabel = {30: "30 minutes", 60: "1 hour", 120: "2 hours"}[minutes];
  advisorAnalyticsScope.textContent = `This gateway · last ${windowLabel}`;
  analyticsPending = true;
  try {
    const stats = await request(`/api/advisor-analytics?minutes=${minutes}`);
    if (Number(advisorAnalyticsWindow.value) !== minutes) return;
    const countFields = ["total_requests", "eligible_requests", "invoked_requests", "successful_advice", "failed_advice", "provider_attempts"];
    if (stats.window_minutes !== minutes || !countFields.every(field => Number.isSafeInteger(stats[field]) && stats[field] >= 0) || stats.invoked_requests > stats.eligible_requests || stats.eligible_requests > stats.total_requests || typeof stats.coverage?.complete !== "boolean") throw new Error("Incompatible activity statistics");
    const percentage = (numerator, denominator) => denominator ? `${(numerator / denominator * 100).toFixed(1)}%` : "--";
    advisorFrequencyStats.textContent = `${stats.invoked_requests} advisor invoked / ${stats.eligible_requests} eligible / ${stats.total_requests} total`;
    advisorAnalyticsRatios.textContent = `Invoked / eligible ${percentage(stats.invoked_requests, stats.eligible_requests)} · Eligible / total ${percentage(stats.eligible_requests, stats.total_requests)} · Invoked / total ${percentage(stats.invoked_requests, stats.total_requests)}`;
    advisorAnalyticsFailures.hidden = !stats.failed_advice && stats.provider_attempts === stats.invoked_requests;
    advisorAnalyticsFailures.textContent = `${stats.failed_advice} advisor failures · ${stats.provider_attempts} provider attempts`;
    const started = new Date(typeof stats.coverage.started_at === "string" ? stats.coverage.started_at : NaN);
    const coverage = stats.coverage.complete ? "Full window verified" : Number.isFinite(started.getTime()) ? `Partial window · coverage verified since ${started.toLocaleString()}` : "Still collecting history";
    advisorAnalyticsCoverage.textContent = `${coverage}${stats.counts_may_be_incomplete ? " · Counts may be incomplete" : ""}`;
  } catch (_) {
    if (Number(advisorAnalyticsWindow.value) === minutes) clearAdvisorAnalytics("Advisor activity unavailable");
  }
  finally {
    analyticsPending = false;
    if (Number(advisorAnalyticsWindow.value) !== minutes && !document.hidden) refreshAdvisorAnalytics();
  }
}

function renderProviderMenu(select, choices, selected, emptyLabel = "No providers configured") {
  const visible = choices.filter(item => item.configured || item.model_id === selected);
  const providers = [...new Set(visible.map(item => item.provider))].sort();
  select.replaceChildren(...providers.map(provider => option(provider, provider)));
  if (!providers.length) select.append(option("", emptyLabel));
  select.value = choices.find(item => item.model_id === selected)?.provider || "";
}

function renderModelMenu(select, choices, provider, selected, emptyLabel = "Choose a model…") {
  const visible = choices.filter(item => item.provider === provider && (item.configured || item.model_id === selected));
  select.replaceChildren();
  if (!selected) select.append(option("", provider ? emptyLabel : "Choose a provider…"));
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
  let help = !model ? "Choose a configured provider and model." : !choice?.configured ? "This saved route needs provider configuration before it can be used." : !efforts.length ? (choice.reasoning_note || "This route has no verified reasoning effort control.") : advisor ? `${choice.access}. Reasoning is independent of the target.` : `${choice.access}. Lower effort favors speed; higher effort allows more reasoning.`;
  if (efforts.length && choice.reasoning_note) help += ` ${choice.reasoning_note}`;
  if (model?.startsWith("gemini-subscription") && !advisor) help += " Text and schema-constrained tool calls; client tools are not run by the subscription. SSE is buffered until completion; vision is unsupported. Output limits are backend-managed; client token caps are not enforced.";
  if (!advisor && routeMode.value === "off") help = "Routing is off. The client chooses the model and reasoning.";
  document.querySelector(helpId).textContent = help;
}

function renderTargetSpeed(selected) {
  const choice = routingModels.find(item => item.model_id === routeModel.value);
  const available = choice?.configured && choice.source_id === "openai-subscription";
  routeFastField.hidden = !available;
  routeFastHelp.hidden = !available;
  routeFast.value = available && selected ? "fast" : "standard";
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
    renderProviderMenu(advisorProvider, advisorModels, routing.state.advisor_model, "No advisors configured");
    renderModelMenu(routeModel, routingModels, routeProvider.value, routing.state.active_model);
    renderModelMenu(routeAdvisor, advisorModels, advisorProvider.value, routing.state.advisor_model, "Choose an advisor model…");
    routeMode.querySelector("[data-unavailable]")?.remove();
    routeMode.value = routing.state.mode;
    const frequencyIndex = frequencyPresets.indexOf(routing.state.advisor_sensitivity);
    advisorFrequency.value = String(frequencyIndex < 0 ? 1 : frequencyIndex);
    renderEfforts(routeEffort, routingModels, routeModel.value, routing.state.reasoning_effort, "#route-effort-help");
    renderTargetSpeed(routing.state.fast_mode);
    renderEfforts(advisorEffort, advisorModels, routeAdvisor.value, routing.state.advisor_reasoning_effort, "#route-advisor-effort-help", true);
    document.querySelector("#advisor-policy-note").textContent = "Selected consultations also call Claude Opus 5.5 Low via Antigravity (max 32,000 input tokens). Oversized context is compacted with Sift, then MiniMax or MiMo.";
    document.querySelector("#advisor-policy-pill").textContent = "Saved per environment";
    routeStatus.textContent = `${routing.client_api_key_required ? "Client API key required" : "No client API key required"} · policy v${routing.state.policy_version}`;
    routeKey.textContent = routing.client_api_key_required ? "CLIENT API KEY REQUIRED" : "LOCAL CLIENT KEY BYPASS";
    setRoutingDisabled(false); routingReady = true;
    refreshAdvisorAnalytics();
  } catch (error) {
    if (!routingModels.length) {
      for (const select of [routeProvider, advisorProvider, routeModel, routeAdvisor, routeEffort, advisorEffort]) select.replaceChildren(option("", "Unavailable"));
      const unavailable = option("", "Policy unavailable"); unavailable.dataset.unavailable = "true"; routeMode.prepend(unavailable); routeMode.value = "";
    }
    setRoutingDisabled(true); routeStatus.textContent = error.message; routeKey.textContent = "GATEWAY UNAVAILABLE";
    clearAdvisorAnalytics("Advisor activity unavailable");
  }
}

async function saveActiveRouting() {
  if (!routingReady) return;
  setRoutingDisabled(true); routeStatus.textContent = "Saving routing…";
  try {
    const state = await request("/api/active-model", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ model: routeModel.value, mode: routeMode.value, reasoning_effort: routeEffort.value || null, fast_mode: !routeFastField.hidden && routeFast.value === "fast" }) });
    routeStatus.textContent = `Routing saved · policy v${state.policy_version}`; setRoutingDisabled(false); refreshAdvisorAnalytics();
  } catch (error) { await renderRouting(); routeStatus.textContent = `Not saved: ${error.message}`; }
}

async function saveAdvisorRouting() {
  if (!routingReady) return;
  const previousFocus = document.activeElement;
  setRoutingDisabled(true); routeStatus.textContent = "Saving advisor…";
  try {
    const state = await request("/api/advisor-model", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ advisor_model: routeAdvisor.value || null, reasoning_effort: advisorEffort.value || null, advisor_sensitivity: frequencyPresets[Number(advisorFrequency.value)] }) });
    routeStatus.textContent = `Advisor saved · policy v${state.policy_version}`; setRoutingDisabled(false); refreshAdvisorAnalytics();
  } catch (error) { await renderRouting(); routeStatus.textContent = `Not saved: ${error.message}`; }
  finally {
    // Disabling a keyboard-operated control moves focus to the document body.
    // Restore it after saving unless the user has focused another element.
    if (previousFocus?.isConnected && !previousFocus.disabled &&
        (document.activeElement === document.body || document.activeElement === previousFocus)) {
      previousFocus.focus({ preventScroll: true });
    }
  }
}

function renderSources(sources, usageMap, usageFallback = "Provider quota data unavailable") {
  const ready = sources.filter(source => source.available && source.configured);
  const more = sources.filter(source => !source.available || !source.configured);
  connectionCount.textContent = `${ready.length} source${ready.length === 1 ? "" : "s"} configured for routing`;
  otherCount.textContent = `${more.length} more provider${more.length === 1 ? "" : "s"} not ready on this gateway`;
  if (!ready.length) cards.replaceChildren(Object.assign(document.createElement("p"), { className: "empty-sources", textContent: "No provider sources are configured for routing. Add credentials to a supported source in the gateway environment." }));
  else cards.replaceChildren(...ready.sort((a, b) => a.name.localeCompare(b.name)).map(source => {
    const node = cardTemplate.content.cloneNode(true), card = node.querySelector(".card"), usage = usageMap[source.id] || { state: "unavailable", detail: usageFallback };
    card.classList.add(source.accent || "blue"); node.querySelector("h3").textContent = source.name; node.querySelector(".kind").textContent = source.kind; node.querySelector(".models").textContent = source.models;
    const line = node.querySelector(".usage"), footer = node.querySelector("footer"), bar = node.querySelector(".bar"), fill = node.querySelector(".bar i"), state = node.querySelector(".source-state");
    state.textContent = "ENV CONFIGURED";
    bar.hidden = usage.state !== "ready";
    if (usage.state === "ready") { const percent = Math.min(100, usage.used / usage.limit * 100), remaining = usage.remaining ?? Math.max(0, usage.limit - usage.used), usd = usage.detail === "USD credits"; line.textContent = `${usd ? `$${remaining.toFixed(2)}` : `${Math.round(remaining)}%`} remaining`; footer.textContent = `${usd ? `$${usage.used.toFixed(2)} used` : `${Math.round(percent)}% used`} · ${compactReset(usage.detail)}`; fill.style.width = `${percent}%`; bar.setAttribute("role", "progressbar"); bar.setAttribute("aria-label", `${source.name} quota consumed`); bar.setAttribute("aria-valuenow", String(Math.round(percent))); bar.setAttribute("aria-valuemin", "0"); bar.setAttribute("aria-valuemax", "100"); }
    else if (usage.state === "connected") { line.textContent = "Subscription connected"; footer.textContent = usage.detail; }
    else if (usage.state === "sign_in_required") { line.textContent = "Sign in required"; footer.textContent = usage.detail; state.textContent = "ACTION NEEDED"; state.classList.add("needs-action"); }
    else { line.textContent = "Usage unavailable"; footer.textContent = usage.detail; }
    return node;
  }));
  moreCards.replaceChildren(...more.sort((a, b) => a.name.localeCompare(b.name)).map(source => {
    const node = cardTemplate.content.cloneNode(true), card = node.querySelector(".card");
    card.classList.add(source.accent || "blue"); node.querySelector("h3").textContent = source.name; node.querySelector(".kind").textContent = source.kind; node.querySelector(".models").textContent = source.models;
    node.querySelector(".usage").textContent = source.available ? "Needs setup" : "Not supported";
    node.querySelector(".source-state").textContent = source.available ? "ADD CREDENTIALS" : "UNAVAILABLE";
    node.querySelector("footer").textContent = source.available ? "Configure its credentials in the gateway environment." : "This source is not currently enabled in the gateway.";
    node.querySelector(".bar").remove();
    return node;
  }));
}

function compactReset(detail) {
  const match = String(detail || "").match(/resets in (?:about )?(\d+) min/i);
  if (!match) return detail || "Provider reported";
  const minutes = Number(match[1]), days = Math.floor(minutes / 1440), hours = Math.floor(minutes % 1440 / 60), remainder = minutes % 60;
  return `resets in ${days ? `${days}d ${hours}h` : `${hours}h ${remainder}m`}`;
}

async function chooseDirectory() { if (!native) return false; const selected = await native.chooseDirectory(); if (!selected) return false; workingDirectory = selected; const field = document.querySelector("#working-directory"); field.textContent = selected; field.title = selected; for (const button of document.querySelectorAll("#agents button")) if (button.textContent === "Choose folder & launch") button.textContent = "Launch"; document.querySelector("#launch-status").textContent = `Selected: ${selected}`; return true; }

async function renderAgents() {
  const agents = native ? await native.listAgents() : agentFallback, area = document.querySelector("#agents"), template = document.querySelector("#agent");
  area.replaceChildren(...agents.sort((a, b) => a.name.localeCompare(b.name)).map(agent => { const node = template.content.cloneNode(true), button = node.querySelector("button"), state = node.querySelector(".agent-state"), ready = agent.installed && agent.ready; node.querySelector("b").textContent = agent.name; node.querySelector("small").textContent = agent.protocol; state.classList.add(native ? (ready ? "installed" : "missing") : "desktop-only"); button.textContent = native ? (ready ? (workingDirectory ? "Launch" : "Choose folder & launch") : agent.installed ? "Check again" : "Install") : "Desktop only"; button.disabled = !native; if (native && agent.installed && !ready) { const message = document.createElement("span"); message.className = "agent-check-status"; message.textContent = "CLI check failed"; node.querySelector(".agent div").append(message); } if (native) button.addEventListener("click", async () => { try { if (agent.installed && !ready) { await renderAgents(); document.querySelector("#launch-status").textContent = "CLI check refreshed. Verify the agent installation and sign-in if its check still fails."; return; } if (!agent.installed) { button.disabled = true; button.textContent = "Opening installer"; const result = await native.installAgent({ id: agent.id }); document.querySelector("#launch-status").textContent = `${result.detail} opened for ${result.name}. Refresh after installation finishes.`; return; } if (!workingDirectory && !await chooseDirectory()) return; const result = await native.launchAgent({ id: agent.id, directory: workingDirectory }); document.querySelector("#launch-status").textContent = `Opened ${result.name} in ${workingDirectory}.`; } catch (error) { document.querySelector("#launch-status").textContent = error.message; button.disabled = false; } }); return node; }));
}

async function load(refreshUsage = false) {
  const button = document.querySelector("#refresh"); button.disabled = true;
  let inventoryReady = false;
  const controls = Promise.all([renderRouting(), renderAgents().catch(() => { document.querySelector("#launch-status").textContent = "Agent checks unavailable. Refresh to retry."; })]);
  try {
    const [sourceResult, usageResult] = await Promise.allSettled([
      request("/api/source-status"), request(`/api/provider-usage${refreshUsage ? "?refresh=true" : ""}`),
    ]);
    if (sourceResult.status === "rejected") {
      connectionCount.textContent = "Provider status unavailable";
      updated.textContent = "Refresh to retry";
      document.querySelector(".connection-summary").classList.add("unavailable");
      cards.replaceChildren(Object.assign(document.createElement("p"), { className: "empty-sources", textContent: `The gateway did not return its provider inventory. ${sourceResult.reason.message || "Check that the gateway is running, then refresh."}` }));
      moreCards.replaceChildren();
      return;
    }
    const sources = sourceResult.value.sources;
    const inventoryValid = Array.isArray(sources) && sources.every(source => source && ["id", "name", "kind", "models"].every(field => typeof source[field] === "string") && typeof source.available === "boolean" && typeof source.configured === "boolean");
    if (!inventoryValid) {
      connectionCount.textContent = "Provider status unavailable";
      updated.textContent = "Refresh after the gateway restarts";
      document.querySelector(".connection-summary").classList.add("unavailable");
      cards.replaceChildren(Object.assign(document.createElement("p"), { className: "empty-sources", textContent: "This gateway returned an incompatible provider inventory. Refresh after the gateway restarts." }));
      moreCards.replaceChildren();
      return;
    }
    const usage = usageResult.status === "fulfilled" ? usageResult.value : { sources: {}, updated_at: null };
    inventoryReady = true;
    renderSources(sources, usage.sources || {}, usageResult.status === "rejected" ? "Usage could not be loaded. Refresh to try again." : "Provider quota data unavailable");
    updated.textContent = usage.updated_at ? `Usage updated ${new Date(usage.updated_at).toLocaleTimeString()}` : "Usage could not be loaded · refresh to retry";
    document.querySelector(".connection-summary").classList.remove("unavailable");
  } finally { await controls; setGatewayStatus(inventoryReady && routingReady); button.disabled = false; }
}
document.querySelector("#gateway").textContent = location.origin;
const localGateway = ["127.0.0.1", "localhost", "[::1]"].includes(location.hostname);
document.querySelector(".privacy-note b").textContent = localGateway ? "Loopback gateway" : "Gateway connection";
document.querySelector(".privacy-note small").textContent = localGateway ? "Bound to this device" : "Opened at this address";
document.querySelector("#surface").textContent = native ? "Desktop control" : "Web control";
document.body.classList.add(native ? "surface-desktop" : "surface-web");
document.querySelector("#launcher-capability").textContent = native ? "NATIVE LAUNCH ENABLED" : "OPEN DESKTOP TO LAUNCH";
document.querySelector("#choose-directory").disabled = !native;
document.querySelector("#refresh").addEventListener("click", () => load(true)); document.querySelector("#choose-directory").addEventListener("click", chooseDirectory);
routeProvider.addEventListener("change", () => {
  renderModelMenu(routeModel, routingModels, routeProvider.value, null);
  renderEfforts(routeEffort, routingModels, "", null, "#route-effort-help");
  renderTargetSpeed(false);
  setRoutingDisabled(false); routeStatus.textContent = "Choose a target model to save this provider.";
});
advisorProvider.addEventListener("change", () => {
  renderModelMenu(routeAdvisor, advisorModels, advisorProvider.value, null, "Choose an advisor model…");
  renderEfforts(advisorEffort, advisorModels, "", null, "#route-advisor-effort-help", true);
  setRoutingDisabled(false);
  if (!advisorProvider.value) saveAdvisorRouting();
  else routeStatus.textContent = "Choose an advisor model to save this provider.";
});
routeModel.addEventListener("change", () => { renderEfforts(routeEffort, routingModels, routeModel.value, null, "#route-effort-help"); renderTargetSpeed(false); saveActiveRouting(); });
routeMode.addEventListener("change", saveActiveRouting);
routeEffort.addEventListener("change", saveActiveRouting);
routeFast.addEventListener("change", saveActiveRouting);
routeAdvisor.addEventListener("change", () => { renderEfforts(advisorEffort, advisorModels, routeAdvisor.value, null, "#route-advisor-effort-help", true); saveAdvisorRouting(); });
advisorEffort.addEventListener("change", saveAdvisorRouting);
advisorFrequency.addEventListener("input", () => renderAdvisorFrequency());
advisorFrequency.addEventListener("change", saveAdvisorRouting);
const menuToggle = document.querySelector("#menu-toggle");
function closeNavigation() {
  document.body.classList.remove("menu-open");
  menuToggle.setAttribute("aria-expanded", "false");
  menuToggle.setAttribute("aria-label", "Open navigation");
}
function showSection() {
  const requested = location.hash.slice(1);
  const section = ["routing", "connections", ...(native ? ["agent-launch"] : [])].includes(requested) ? requested : "routing";
  const titles = { routing: "Routing", connections: "Connections", "agent-launch": "Coding agents" };
  const descriptions = { routing: "Choose the target model and optional advisor for new requests.", connections: "Provider status and quota, reported by each configured source.", "agent-launch": "Open a coding session in your selected project folder." };
  for (const id of ["routing", "connections", "agent-launch"]) document.getElementById(id).hidden = id !== section;
  for (const link of document.querySelectorAll("[data-view]")) {
    const active = link.dataset.view === section;
    link.classList.toggle("active", active);
    if (active) link.setAttribute("aria-current", "page"); else link.removeAttribute("aria-current");
  }
  document.querySelector("#page-title").textContent = titles[section];
  document.querySelector("#breadcrumb-page").textContent = titles[section];
  document.querySelector("#page-description").textContent = descriptions[section];
  closeNavigation();
}
menuToggle.addEventListener("click", () => {
  const open = document.body.classList.toggle("menu-open");
  menuToggle.setAttribute("aria-expanded", String(open));
  menuToggle.setAttribute("aria-label", open ? "Close navigation" : "Open navigation");
});
document.querySelectorAll("[data-view]").forEach(link => link.addEventListener("click", event => { event.preventDefault(); location.hash = link.dataset.view; showSection(); document.querySelector("#page-title").focus({preventScroll:true}); window.scrollTo(0,0); }));
document.addEventListener("keydown", event => { if (event.key === "Escape" && document.body.classList.contains("menu-open")) { closeNavigation(); menuToggle.focus(); } });
window.addEventListener("hashchange", showSection);
advisorAnalyticsWindow.addEventListener("change", () => { clearAdvisorAnalytics("Loading advisor activity…"); refreshAdvisorAnalytics(); });
setInterval(() => { if (!document.hidden) refreshAdvisorAnalytics(); }, 10000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshAdvisorAnalytics(); });
showSection();
load(false);
