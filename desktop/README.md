# Subroute Desktop

This is the native shell for the shared Subroute Control Desk. Both Electron and `/control` load the same HTML, CSS, JavaScript, provider inventory, routing state, and usage data from the gateway. Electron adds only the host capabilities that a browser cannot provide: folder selection, agent installation, and terminal launch. Docker Compose does not start or depend on Electron.

```powershell
cd desktop
npm install
npm start
```

On first launch it creates `%APPDATA%/subroute-desktop/sources.json` from `sources.example.json`. That file contains only the loopback gateway URL. If the gateway is not running or the address needs to change, Desktop opens a connection screen with a local address check and retry. The address is saved only after the gateway responds. Provider inventory and usage adapters are owned by the gateway, so they cannot drift between the browser and Desktop interfaces. Do not place API keys in the Desktop file.

## Launch agents

The launcher starts each installed CLI in a visible PowerShell window rooted at the selected working directory. It checks the CLI's local `--version` command before enabling its Launch button. Claude Code, Codex, OpenCode, Hermes, and DeepSeek Harness receive a session-only Subroute configuration; their generated profile files live under `%APPDATA%/subroute-desktop/launch/`, not in the agent's existing home directory. The terminal stays open so a gateway or provider error remains visible instead of being reported as a false successful launch.
