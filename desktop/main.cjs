"use strict";
const { app, BrowserWindow, ipcMain } = require("electron");
const fs = require("node:fs/promises");
const path = require("node:path");
const { parseUsage } = require("./usage.cjs");
const { availability, launch } = require("./agents.cjs");

const examplePath = path.join(__dirname, "sources.example.json");
const sourcePath = () => path.join(app.getPath("userData"), "sources.json");

async function readSources() {
  try { return JSON.parse(await fs.readFile(sourcePath(), "utf8")); }
  catch (error) {
    if (error.code !== "ENOENT") throw error;
    const initial = await fs.readFile(examplePath, "utf8");
    await fs.writeFile(sourcePath(), initial, "utf8");
    return JSON.parse(initial);
  }
}

async function refreshUsage() {
  const config = await readSources();
  let statuses = new Map();
  try {
    const response = await fetch(`${config.gatewayUrl}/api/source-status`, { signal: AbortSignal.timeout(3000) });
    const payload = response.ok ? await response.json() : { sources: [] };
    statuses = new Map((payload.sources || []).map(source => [source.name.toLowerCase(), source.configured]));
  } catch { /* Gateway status is optional for the desktop companion. */ }
  return Promise.all(config.sources.map(async (source) => {
    const configured = statuses.get(source.name.toLowerCase());
    if (!source.usage) return { ...source, configured, usage: parseUsage(source, {}) };
    try {
      const response = await fetch(source.usage.url, { headers: { accept: "application/json" }, signal: AbortSignal.timeout(5000) });
      if (!response.ok) return { ...source, configured, usage: { state: "unavailable", detail: `Usage endpoint returned ${response.status}` } };
      return { ...source, configured, usage: parseUsage(source, await response.json()) };
    } catch { return { ...source, configured, usage: { state: "unavailable", detail: "Usage endpoint is unreachable" } }; }
  }));
}

app.whenReady().then(() => {
  ipcMain.handle("sources:read", readSources);
  ipcMain.handle("usage:refresh", refreshUsage);
  ipcMain.handle("agents:list", availability);
  ipcMain.handle("agents:directory", async () => (await require("electron").dialog.showOpenDialog({ properties: ["openDirectory", "createDirectory"] })).filePaths[0] || null);
  ipcMain.handle("agents:launch", async (_, request) => {
    const config = await readSources();
    return launch(request.id, request.directory, config.gatewayUrl);
  });
  const window = new BrowserWindow({ width: 1080, height: 760, minWidth: 820, minHeight: 620, backgroundColor: "#10151b", webPreferences: { preload: path.join(__dirname, "preload.cjs"), contextIsolation: true, nodeIntegration: false } });
  window.loadFile("index.html");
});
app.on("window-all-closed", () => { if (process.platform !== "darwin") app.quit(); });
