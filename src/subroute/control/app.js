const form = document.querySelector("#routing");
const modelSelect = document.querySelector("#model");
const effortSelect = document.querySelector("#effort");
const fast = document.querySelector("#fast");
const save = document.querySelector("#save");
const refresh = document.querySelector("#refresh");
const status = document.querySelector("#status");
let models = [];

async function request(path, options) {
  const response = await fetch(path, options);
  const result = await response.json();
  if (!response.ok) throw new Error(result.detail || `Request failed (${response.status})`);
  return result;
}

function showSettings(state = {}) {
  const model = models.find(item => item.model_id === modelSelect.value);
  const efforts = model?.reasoning_efforts || [];
  effortSelect.replaceChildren(new Option("Default", ""), ...efforts.map(effort => new Option(effort, effort)));
  effortSelect.value = efforts.includes(state.reasoning_effort) ? state.reasoning_effort : "";
  document.querySelector("#effort-field").hidden = !efforts.length;
  document.querySelector("#speed-field").hidden = model?.source_id !== "openai-subscription";
  fast.checked = state.fast_mode === true;
}

async function load() {
  const result = await request("/api/routing-options");
  models = result.models;
  const groups = new Map();
  for (const model of models) {
    if (!groups.has(model.provider)) {
      const group = document.createElement("optgroup");
      group.label = model.provider;
      groups.set(model.provider, group);
    }
    const name = model.display_name === model.model_id ? model.model_id : `${model.display_name} (${model.model_id})`;
    groups.get(model.provider).append(new Option(name, model.model_id));
  }
  modelSelect.replaceChildren(...groups.values());
  modelSelect.value = result.state.active_model;
  showSettings(result.state);
}

function setBusy(busy) {
  for (const control of [modelSelect, effortSelect, fast, save, refresh]) control.disabled = busy;
}

modelSelect.addEventListener("change", () => showSettings());
form.addEventListener("submit", async event => {
  event.preventDefault();
  setBusy(true);
  status.textContent = "Saving…";
  try {
    const state = await request("/api/active-model", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model: modelSelect.value, reasoning_effort: effortSelect.value || null, fast_mode: fast.checked }),
    });
    showSettings(state);
    status.textContent = `Saved ${state.active_model}.`;
  } catch (error) {
    status.textContent = `Could not save. ${error.message}`;
  } finally {
    setBusy(false);
  }
});

refresh.addEventListener("click", async () => {
  setBusy(true);
  status.textContent = "Refreshing subscription models…";
  try {
    const result = await request("/api/model-catalog/refresh", { method: "POST" });
    await load();
    status.textContent = Object.entries({ openai: "Codex", gemini: "Antigravity" }).map(([key, name]) =>
      result[key]?.state === "ready" ? `${name} refreshed.` : `${name} refresh unavailable.`
    ).join(" ");
  } catch (error) {
    status.textContent = `Could not refresh. ${error.message}`;
  } finally {
    setBusy(false);
  }
});

load().then(() => {
  status.textContent = "";
  setBusy(false);
}).catch(error => {
  status.textContent = `Could not load models. ${error.message}`;
});
