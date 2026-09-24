"use strict";
const { app, BrowserWindow, ipcMain } = require("electron");
const fs = require("node:fs/promises");
const path = require("node:path");
const { pathToFileURL } = require("node:url");
const { availability, install, launch } = require("./agents.cjs");

const examplePath = path.join(__dirname, "sources.example.json");
const sourcePath = () => path.join(app.getPath("userData"), "sources.json");
const connectionPage = path.join(__dirname, "connect.html");
const trustedConnectionPage = pathToFileURL(connectionPage).href;

function normalizeGatewayUrl(value) {
  const url = new URL(String(value || "").trim());
  if (url.protocol !== "http:" || !["127.0.0.1", "localhost", "[::1]"].includes(url.hostname)) {
    throw new Error("Use an HTTP gateway running on this device, such as http://127.0.0.1:4000.");
  }
  if (url.username || url.password) throw new Error("Do not include credentials in the gateway address.");
  return url.origin;
}

async function readSources() {
  try { return JSON.parse(await fs.readFile(sourcePath(), "utf8")); }
  catch (error) {
    if (error.code !== "ENOENT") throw error;
    const initial = await fs.readFile(examplePath, "utf8");
    await fs.writeFile(sourcePath(), initial, "utf8");
    return JSON.parse(initial);
  }
}

function controlUrl(config) {
  return new URL("/control?surface=desktop", normalizeGatewayUrl(config.gatewayUrl)).toString();
}

app.whenReady().then(() => {
  app.setName("Subroute");
  ipcMain.handle("agents:list", availability);
  ipcMain.handle("agents:install", async (_, request) => install(request.id, app.getPath("home")));
  ipcMain.handle("agents:directory", async () => (await require("electron").dialog.showOpenDialog({ properties: ["openDirectory", "createDirectory"] })).filePaths[0] || null);
  ipcMain.handle("gateway:current", async event => {
    if (!event.sender.getURL().startsWith(`${trustedConnectionPage}?`)) throw new Error("Gateway settings are only available on the local connection screen.");
    return (await readSources()).gatewayUrl;
  });
  ipcMain.handle("gateway:connect", async (event, request) => {
    if (!event.sender.getURL().startsWith(`${trustedConnectionPage}?`)) throw new Error("Gateway settings are only available on the local connection screen.");
    const gatewayUrl = normalizeGatewayUrl(request?.gatewayUrl);
    const sender = BrowserWindow.fromWebContents(event.sender);
    try {
      const response = await fetch(new URL("/api/source-status", gatewayUrl), { signal: AbortSignal.timeout(4000) });
      if (!response.ok) throw new Error(`Gateway returned HTTP ${response.status}.`);
      const payload = await response.json().catch(() => null);
      const validInventory = Array.isArray(payload?.sources) && payload.sources.length > 0 && payload.sources.every(source =>
        source && ["id", "name", "kind", "models"].every(field => typeof source[field] === "string")
          && typeof source.available === "boolean" && typeof source.configured === "boolean",
      );
      if (!validInventory) throw new Error("That service responded, but it did not return a Subroute provider inventory.");
      const config = { gatewayUrl };
      const destination = sourcePath();
      await fs.mkdir(path.dirname(destination), { recursive: true });
      const temporary = `${destination}.${process.pid}.tmp`;
      await fs.writeFile(temporary, `${JSON.stringify(config, null, 2)}\n`, "utf8");
      await fs.rename(temporary, destination);
      if (sender && !sender.isDestroyed()) await sender.loadURL(controlUrl(config));
      return { ok: true };
    } catch (error) {
      throw new Error(error.name === "TimeoutError" || error.name === "AbortError" ? "No response from that gateway. Check the address and make sure Docker Compose is running." : error.message || "Could not connect to the gateway.");
    }
  });
  ipcMain.handle("agents:launch", async (_, request) => {
    const config = await readSources();
    return launch(request.id, request.directory, config.gatewayUrl);
  });
  const window = new BrowserWindow({ title: "Subroute | Local AI gateway", width: 1240, height: 900, minWidth: 900, minHeight: 680, backgroundColor: "#f5f3ec", webPreferences: { preload: path.join(__dirname, "preload.cjs"), contextIsolation: true, nodeIntegration: false } });
  let attemptedGateway = "http://127.0.0.1:4000";
  const showConnectionPage = async (gatewayUrl, error) => {
    if (!window.isDestroyed()) await window.loadFile(connectionPage, { query: { gatewayUrl, error: error || "" } });
  };
  window.webContents.on("did-fail-load", (_, errorCode, description, validatedURL, isMainFrame) => {
    if (isMainFrame && errorCode !== -3 && validatedURL.startsWith("http")) showConnectionPage(attemptedGateway, description).catch(() => {});
  });
  window.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
  readSources().then(config => { attemptedGateway = config.gatewayUrl; return window.loadURL(controlUrl(config)).catch(error => showConnectionPage(config.gatewayUrl, error.message)); })
    .catch(error => showConnectionPage("http://127.0.0.1:4000", error.message));
});
app.on("window-all-closed", () => { if (process.platform !== "darwin") app.quit(); });
