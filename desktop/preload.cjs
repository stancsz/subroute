"use strict";
const { contextBridge, ipcRenderer } = require("electron");
contextBridge.exposeInMainWorld("subroute", { readSources: () => ipcRenderer.invoke("sources:read"), refreshUsage: () => ipcRenderer.invoke("usage:refresh"), listAgents: () => ipcRenderer.invoke("agents:list"), chooseDirectory: () => ipcRenderer.invoke("agents:directory"), launchAgent: request => ipcRenderer.invoke("agents:launch", request) });
