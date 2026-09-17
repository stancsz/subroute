"use strict";
const { app, BrowserWindow, ipcMain } = require("electron");
const fs = require("node:fs/promises");
const path = require("node:path");
const { availability, install, launch } = require("./agents.cjs");

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

function controlUrl(config) {
  const url = new URL(config.gatewayUrl);
  if (url.protocol !== "http:" || !["127.0.0.1", "localhost", "[::1]"].includes(url.hostname)) {
    throw new Error("Desktop control requires a loopback HTTP gateway URL");
  }
  return new URL("/control?surface=desktop", url).toString();
}

app.whenReady().then(() => {
  ipcMain.handle("agents:list", availability);
  ipcMain.handle("agents:install", async (_, request) => install(request.id, app.getPath("home")));
  ipcMain.handle("agents:directory", async () => (await require("electron").dialog.showOpenDialog({ properties: ["openDirectory", "createDirectory"] })).filePaths[0] || null);
  ipcMain.handle("agents:launch", async (_, request) => {
    const config = await readSources();
    return launch(request.id, request.directory, config.gatewayUrl);
  });
  const window = new BrowserWindow({ width: 1080, height: 760, minWidth: 820, minHeight: 620, backgroundColor: "#10151b", webPreferences: { preload: path.join(__dirname, "preload.cjs"), contextIsolation: true, nodeIntegration: false } });
  readSources().then(config => window.loadURL(controlUrl(config)));
});
app.on("window-all-closed", () => { if (process.platform !== "darwin") app.quit(); });
