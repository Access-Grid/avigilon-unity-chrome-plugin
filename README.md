# Avigilon Unity Chrome Plugin

Chrome extension + companion bridge app that synchronizes the Avigilon Unity (Plasec) access control database with [AccessGrid](https://accessgrid.com) mobile credentials.

Designed to run on multiple machines simultaneously — the sync engine is fully stateless, comparing live data from both systems on every cycle rather than relying on local state.

## Architecture

```
Chrome Extension (MV3 service worker)
    ↕  AccessGrid API (direct, via host_permissions)
    ↕  fetch('http://localhost:19780/api/...')
Python/Tk Bridge App (localhost HTTP server)
    ↕  requests (SSL verify=False for self-signed certs)
    ↕  XML parsing → JSON normalization
Avigilon / Plasec Server
```

**Why two components?** The Plasec server uses self-signed SSL certificates and returns XML responses. Chrome's `fetch()` cannot bypass SSL verification, so the bridge app handles SSL and XML, exposing a clean JSON API on localhost that the extension consumes.

## Sync Phases

On every cycle (triggered by page navigation or 1-minute timer), the extension runs 6 phases:

| Phase | Direction | Action |
|-------|-----------|--------|
| 1 | Plasec → AccessGrid | Provision new mobile credentials for tokens marked "AccessGrid" |
| 2 | Plasec → AccessGrid | Push token status changes (active/inactive/expired) |
| 3 | Plasec → AccessGrid | Terminate AG cards when identities or tokens are deleted |
| 4 | AccessGrid → Plasec | Sync AG card state changes back to Plasec token statuses |
| 5 | — | Retries (implicit — stateless design retries on next cycle) |
| 6 | Plasec → AccessGrid | Push contact field changes (name, title) |

## Setup

### 1. Install the Bridge App

Download the latest build from [Releases](../../releases) for your platform:

- **macOS**: `AvigilonBridge-macos.zip` — unzip and move to Applications
- **Windows**: `AvigilonBridge-windows.exe`
- **Linux**: `AvigilonBridge-linux.tar.gz`

Or run from source:

```bash
cd bridge
python -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python main.py
```

On launch, configure:
- **Plasec Host/IP** — the Avigilon Unity server address
- **Username / Password** — Plasec admin credentials
- Check **"Start bridge on login"** to run automatically on boot

The bridge runs on `localhost:19780`.

### 2. Install the Chrome Extension

Download `AvigilonUnitySync-chrome-extension.zip` from [Releases](../../releases), or load from source:

1. Open `chrome://extensions/`
2. Enable **Developer mode**
3. Click **Load unpacked** → select the `chrome-extension/` directory

### 3. Configure AccessGrid

Click the extension icon in Chrome and go to **Settings**:
- **Account ID** — your AccessGrid account ID
- **API Secret** — your AccessGrid API secret
- **Template ID** — the card template to provision against

### 4. Verify

Click **Run Sync Now** in the extension popup. The status panel shows identity count, AG card count, and actions taken.

## Fake Avigilon Server

For local development and testing, a mock Plasec server is included:

```bash
cd fake-avigilon-server

# HTTPS with auto-generated self-signed cert
python server.py --port 8443

# HTTP only
python server.py --no-ssl --port 9443
```

Seeds 10 identities (8 active with AccessGrid tokens), 3 card formats. Accepts any login credentials. Implements all JSON and XML endpoints.

## Development

### Bridge Tests

```bash
cd bridge
.venv/bin/python -m pytest tests/ -v
```

### Project Structure

```
chrome-extension/                 Chrome MV3 extension
├── manifest.json                 Permissions, service worker config
├── service-worker.js             Stateless sync engine (6 phases)
├── accessgrid-sdk.js             AccessGrid JS SDK (fetch + Web Crypto)
├── popup.html / popup.js         Status dashboard + AG config UI
└── icons/

bridge/                           Python/Tk companion app
├── main.py                       Entry point, Tk settings window, tray icon
├── src/
│   ├── plasec_client.py          Plasec HTTP client (SSL bypass, XML parsing)
│   ├── server.py                 Flask localhost API (JSON proxy)
│   ├── config.py                 Fernet-encrypted config storage
│   ├── tray.py                   System tray icon (pystray)
│   ├── autostart.py              OS-specific auto-start registration
│   └── constants.py              Status codes, mappings
├── tests/                        30 tests
├── AvigilonBridge.spec           PyInstaller build spec
└── requirements.txt

fake-avigilon-server/             Mock Plasec server for testing
└── server.py

.github/workflows/build.yml      CI: bridge binaries (3 OS) + extension zip
```

## CI/CD

On every push to `main`, GitHub Actions:
1. Builds the bridge binary for macOS, Linux, and Windows (PyInstaller + UPX)
2. Packages the Chrome extension as a zip
3. Runs the test suite
4. Publishes all artifacts to a rolling `latest` release
