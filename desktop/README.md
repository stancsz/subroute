# Subroute Desktop

This is a separate Electron companion. It never imports the gateway router and has no route-changing IPC command. Docker Compose does not mount, start, or depend on this directory.

```powershell
cd desktop
npm install
npm start
```

On first launch it creates `%APPDATA%/subroute-desktop/sources.json` from `sources.example.json`. A usage adapter is optional and read-only:

```json
"usage": { "url": "http://127.0.0.1:9000/quota", "usedField": "used", "limitField": "limit" }
```

Only provider-reported fields are displayed. Do not place API keys in this file. If a provider requires authentication, expose a loopback-only local adapter that owns its credential handling.
