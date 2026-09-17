const cards = document.querySelector("#cards"), template = document.querySelector("#card"), updated = document.querySelector("#updated");
let workingDirectory = null;
async function renderAgents() {
  const agents = await window.subroute.listAgents(), area = document.querySelector("#agents"), template = document.querySelector("#agent");
  area.replaceChildren(...agents.map(agent => { const node = template.content.cloneNode(true), button = node.querySelector("button"); node.querySelector("b").textContent = agent.name; node.querySelector("small").textContent = agent.protocol; node.querySelector(".agent-state").classList.add(agent.installed ? "installed" : "missing"); button.textContent = agent.installed ? "Launch" : "Not installed"; button.disabled = !agent.installed; button.addEventListener("click", async () => { if (!workingDirectory) { workingDirectory = await window.subroute.chooseDirectory(); if (!workingDirectory) return; document.querySelector("#working-directory").textContent = workingDirectory; } try { const result = await window.subroute.launchAgent({ id: agent.id, directory: workingDirectory }); document.querySelector("#launch-status").textContent = `Launched ${result.name}: ${result.command}`; } catch (error) { document.querySelector("#launch-status").textContent = error.message; } }); return node; }));
}
function render(sources) {
  cards.replaceChildren(...sources.map(source => {
    const node = template.content.cloneNode(true), card = node.querySelector(".card"), usage = source.usage;
    card.classList.add(source.accent || "blue"); node.querySelector("h2").textContent = source.name; node.querySelector(".kind").textContent = source.kind;
    const footer = node.querySelector("footer"), line = node.querySelector(".usage"), bar = node.querySelector("i");
    const connection = source.configured === true ? "Configured" : source.configured === false ? "Credential required" : "Gateway status unavailable";
    if (usage.state === "ready") { const percent = Math.min(100, usage.used / usage.limit * 100); line.textContent = `${connection} · ${usage.remaining} remaining of ${usage.limit}`; footer.textContent = `${Math.round(percent)}% used · provider reported`; bar.style.width = `${percent}%`; }
    else { line.textContent = `${connection} · usage unavailable`; footer.textContent = usage.detail; bar.style.width = "0%"; }
    return node;
  })); updated.textContent = `Updated ${new Date().toLocaleTimeString()}`;
}
async function load() { document.querySelector("#refresh").disabled = true; const sources = await window.subroute.refreshUsage(); render(sources); document.querySelector("#refresh").disabled = false; }
window.subroute.readSources().then(config => document.querySelector("#gateway").textContent = config.gatewayUrl).then(load); renderAgents(); document.querySelector("#refresh").addEventListener("click", load);
