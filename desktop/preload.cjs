"use strict";
const { contextBridge, ipcRenderer } = require("electron");
contextBridge.exposeInMainWorld("subroute", { listAgents: () => ipcRenderer.invoke("agents:list"), chooseDirectory: () => ipcRenderer.invoke("agents:directory"), installAgent: request => ipcRenderer.invoke("agents:install", request), launchAgent: request => ipcRenderer.invoke("agents:launch", request) });
