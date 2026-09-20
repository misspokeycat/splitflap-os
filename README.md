# SplitFlap OS

A web-based control interface for modular split-flap displays. Manage apps, compose messages, and calibrate hardware from any device.

Built on [Adam G Makes' Split-Flap Display](https://github.com/adamgmakes/SplitFlapDisplay) hardware platform.

## Features

- **40+ apps** — weather, stocks, sports scores, crypto, word clock, trivia, news headlines, quotes, and more
- **App Library** — browse and install apps by category (time, entertainment, news, lifestyle, education, finance, sports)
- **Compose** — click-to-type grid editor with color tile support
- **Playlists** — sequence apps and composed messages with per-entry timing and transitions
- **Live preview** — animated flap simulation in the browser
- **Calibration tools** — hardware inspector, auto fine-tune, teach mode
- **Universal Firmware provisioning** — automatically discover, identify, assign, diagnose, and de-provision modules from the calibration page
- **MQTT** — Home Assistant integration with auto-discovery
- **Configurable serial port** — auto-detect available ports or enter a custom path; supports env var, settings UI, and Docker deployments
- **SplitFlap Gateway (MQTT)** — alternatively drive the display remotely through an ESP32 gateway over MQTT instead of a local serial port; configure broker, port, topic prefix, and optional credentials in the settings UI
- **WiFi hotspot fallback** — Pi creates its own network when no WiFi is found, so you can always access the UI
- **Offline resilience** — internet-dependent apps degrade gracefully, offline apps keep running
- **Plugin architecture** — community apps via manifest + fetch pattern
- **Mobile-friendly** — hamburger menu, sticky bottom tabs, responsive layout


<img width="250" alt="Apps" src="https://github.com/user-attachments/assets/002634e8-00d5-48ae-97a3-5d3ee3ee0ad4" />  <img width="250" alt="Compose" src="https://github.com/user-attachments/assets/13aa01ed-aaa7-48e3-8c95-bbb158ab4fe4" />  <img width="250" alt="Playlists" src="https://github.com/user-attachments/assets/463dc2ca-ffff-4560-a4f0-7409b86f03e4" /> 

<img width="800" alt="Desktop" src="https://github.com/user-attachments/assets/5d67f2aa-ea38-4db9-a7fd-6181e8535f1b" />


## Quick Start

On your Split-Flap Display's Raspberry Pi:

> **Raspberry Pi OS Lite users:** git may not be pre-installed. Run this first:
> ```bash
> sudo apt-get update && sudo apt-get install -y git
> ```

```bash
git clone https://github.com/csader/splitflap-os.git
cd splitflap-os
sudo bash setup/install.sh
```

The installer sets up auto-start, WiFi hotspot fallback, and all dependencies. Access the UI at `http://<your-pi-ip>`.

If no WiFi is available, the Pi creates a hotspot:
- SSID: `SplitflapOS`
- Password: `splitflap`
- UI: `http://192.168.4.1`

Configure WiFi from Settings > WiFi / Network in the UI.

## Updating

```bash
cd ~/splitflap-os
git pull origin main
sudo bash setup/install.sh
```

## Hardware

This project is the **web UI only**. For the firmware and physical display hardware (3D printed parts, PCBs, BOM), see the original project by Adam G Makes:

**https://github.com/adamgmakes/SplitFlapDisplay**

## SplitFlap Gateway (MQTT)

Instead of a local serial connection, SplitFlap OS can drive the display through a **SplitFlap Gateway** (an ESP32 running `SplitFlapGateway_2.ino`) over MQTT. This is useful when the display and the server aren't physically wired together.

Switch modes under **Settings > Hardware Connection > Connection Type**. When "SplitFlap Gateway (MQTT)" is selected, enter the broker address, port, topic prefix, and optional username/password, then click **Connect Gateway**.

SplitFlap OS only interacts with two topics, so the impact on the rest of the system is minimal:

| Topic | Direction | Payload |
|-------|-----------|---------|
| `<prefix>/send` | OS → gateway | Raw RS485 frame (e.g. `m05-A\n`), forwarded verbatim to the bus |
| `<prefix>/rx`   | gateway → OS | JSON `{"command":"<frame>", ...}` for each frame received from a module |

The gateway transport presents the same interface as a serial port internally, so calibration, EEPROM sync, and all display writes work identically in either mode.

These settings can also be supplied via environment variables (useful for Docker/headless deployments), which take precedence over the settings file:

```
SPLITFLAP_CONNECTION_TYPE=gateway     # 'serial' (default) or 'gateway'
SPLITFLAP_GATEWAY_BROKER=192.168.1.50
SPLITFLAP_GATEWAY_PORT=1883
SPLITFLAP_GATEWAY_PREFIX=splitflap
SPLITFLAP_GATEWAY_USER=               # optional
SPLITFLAP_GATEWAY_PASSWORD=           # optional
```

> Note: this MQTT connection (display transport) is independent of the existing **MQTT / Home Assistant** integration (state publishing & control), which continues to use its own broker settings.
## Universal Firmware

Universal Firmware is an **optional alternative** to the original per-module
firmware. It lets every module run the same firmware image instead of compiling
a different build for each module ID. IDs are assigned later over RS-485,
making it much easier to add, replace, or rearrange modules.

You can find and download the universal version of the firmware from
[avandeputte/SplitFlapUniversalFirmware](https://github.com/avandeputte/SplitFlapUniversalFirmware).
When it is used, Splitflap OS can discover new modules, identify them by moving
the reel, assign IDs, and run supported diagnostics from the Calibration page.
The original module firmware remains supported.

Learn more: [firmware and protocol documentation](https://github.com/avandeputte/SplitFlapUniversalFirmware/blob/main/README.md)
| [`provision.py` example](https://github.com/avandeputte/SplitFlapUniversalFirmware/blob/main/provision.py)
| [architecture notes](https://github.com/avandeputte/SplitFlapUniversalFirmware/blob/main/ARCHITECTURE.md)

## Repo Structure

```
server/          — Flask web app (backend + frontend)
apps/            — Plugin library (all installable apps)
setup/           — Raspberry Pi setup scripts and systemd services
tests/           — Test suite (see Development below)
```

Inside `server/`:

```
app.py                    — entry point: creates the Flask app, registers blueprints
splitflap/
  settings.py             — defaults, settings.json load/save, accessors
  grid.py                 — grid geometry and text layout
  state.py                — RuntimeState: the state shared across threads
  transport.py            — the wire to the display (serial or MQTT gateway)
  module_registry.py      — which chip serial answers to which module ID
  display.py              — rendering text onto the modules
  animations.py           — module send orders for transitions
  plugins.py              — the app plugin system
  playlist.py             — the display loop
  scheduler.py            — schedules and quiet hours
  triggers.py             — app-driven interrupts
  notifications.py        — the /notify queue
  network.py              — connectivity probing
  mqtt.py                 — Home Assistant integration
  startup.py              — boot tasks (auto-home, broker connect, module watch)
  tasks.py                — background loop registration
  web/                    — HTTP routes, one blueprint per area of the UI
hardware/                 — Universal Firmware provisioning
gateway_transport.py      — pyserial-compatible facade over an MQTT gateway
```

Runtime state lives on the single `RuntimeState` object in `state.py`, which
the display loop, the scheduler, the trigger loop and the request handlers all
read and write. Import it as `from splitflap.state import state` and access
attributes (`state.active_app`); rebinding a `from ... import` name would only
change your module's copy.

## HTTP API

No authentication — the server assumes a trusted LAN. `/notify` is the one
exception, and only because it predates the rest.

Set the display:

```bash
curl -X POST http://splitflap.local/update_playlist \
  -H 'Content-Type: application/json' \
  -d '{"text": "HELLO|WORLD"}'
```

`|` starts a new line, lines are centred to fill the grid, and anything past
the last row is dropped. `{"center": false}` left-aligns instead. Pass a list
for a rotation, with `delay` seconds per page:

```bash
-d '{"text": ["FIRST", "SECOND"], "delay": 10}'
```

`{"pages": [...]}` is the lower-level form the web UI uses: strings written to
the modules as-is, so the caller lays the grid out itself. A page may also be
an object — `{"text": ..., "delay": ..., "style": ..., "speed": ...}` — for
per-page timing and transition. When both are given, `pages` wins.

Other endpoints:

| | |
|---|---|
| `POST /run_app` | `{"app": "weather"}` — start an installed app |
| `POST /stop_app` | stop it |
| `POST /run_app_playlist` | `{"name": "..."}` — start a saved app playlist |
| `GET /current_state` | what is on the display now |
| `GET /grid_config` | `{rows, cols, total}` |
| `GET /installed_apps` | the app list and their settings schema |
| `POST /notify` | temporary interrupt; needs a bearer token from `notify_sources` |
| `POST /module_audit` | compare module EEPROM against settings.json; writes nothing |

### EEPROM drift

Modules keep their offset, calibration and per-character tuning in EEPROM, and
it drifts: a write interrupted by a brownout leaves a half-written cell, and
the motors draw hardest exactly when a write lands. An unwritten cell reads as
65535 — which is also this project's "no tuning stored" sentinel, since
`w<index>:65535` is the erase command.

A stored step is a position within one revolution, so anything at or beyond
the module's calibration cannot be real. Values that fail that test are
dropped on sync rather than written into settings.json, and a module reporting
an unusable calibration keeps the one already stored — otherwise a bad reading
would be written back to the module on the next restore.

`POST /module_audit` reports the state of each module without changing
anything:

```bash
curl -X POST http://splitflap.local/module_audit \
  -H 'Content-Type: application/json' -d '{"ids": [0, 1, 2]}'
```

Each module comes back as `ok`, `diverged` (disagrees with settings.json),
`suspect` (holds values that cannot be right), `unusable`, or `no_response`.
Omit `ids` to audit the whole display.

### When a module forgets its ID

A Universal Firmware module's ID lives in that same EEPROM. A module that
loses it stops answering to its address and starts advertising for a new one,
so its place in the display goes blank until someone opens the calibration
page and assigns it again by hand.

The chip serial cannot be lost — it is burned into the microcontroller — so
Splitflap OS records which serial answers to which ID as it sees them on the
bus, under `module_registry` in settings.json. When a module it recognises
starts asking for an ID, it checks that nothing else is answering to that ID,
hands back the one that module had, and writes its stored offset, calibration
and per-character tuning back onto it.

It never takes an ID from a module still using it, and it gives up after three
tries, so a module that cannot hold a write does not become a write every
fifteen seconds. De-provisioning a module from the calibration page forgets
it too: an ID erased on purpose stays erased.

Each module's card on the calibration page counts how often this has happened
to it. A module that needs it repeatedly has failing EEPROM and wants
replacing. The switch at the top of the Provision card turns the whole thing
off (`auto_reprovision` in settings.json).

The same text form reaches the display over MQTT, if the Home Assistant
integration is enabled:

```bash
mosquitto_pub -t splitflap/text/set -m 'HELLO|WORLD'
```

## Development

```bash
python3 -m venv venv
venv/bin/pip install -r server/requirements.txt -r requirements-dev.txt
venv/bin/python -m pytest tests/ -q
```

The browser-side tests under `tests/js/` run through the same command via
node, and are skipped if node is not installed.

Importing `server/app.py` brings the whole server up: it opens the serial port,
homes the display, starts four background loops and connects to the broker. Set
`SPLITFLAP_NO_BACKGROUND_TASKS=1` to import it without any of that — the test
suite does, along with `SPLITFLAP_CONFIG` pointed at a throwaway file.

## Creating an App

Each app is a directory in `apps/` with:

- `manifest.json` for metadata and settings schema
- `app.py` with a `fetch(settings, format_lines, get_rows, get_cols)` function

`fetch()` returns a list of page strings. Each page shows for `loop_delay` seconds, and results are cached by `refresh_interval`.

For full app-development documentation, settings schema, and examples, see [APPS_README.md](APPS_README.md).

## Attribution

- Splitflap OS is based on the
  [Split-Flap Display](https://github.com/adamgmakes/SplitFlapDisplay) by
  **Adam G Makes**, licensed under
  [CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/).

- Thanks to **[@avandeputte](https://github.com/avandeputte)** for
  [SplitFlapUniversalFirmware](https://github.com/avandeputte/SplitFlapUniversalFirmware),
  its documented RS-485 provisioning protocol and `provision.py` reference, and
  [SplitFlapGateway](https://github.com/avandeputte/SplitFlapGateway), whose
  [web UI](https://github.com/avandeputte/SplitFlapGateway#web-ui) helped inform
  this independent provisioning interface.

## License

[Creative Commons Attribution-NonCommercial-ShareAlike 4.0 International (CC BY-NC-SA 4.0)](https://creativecommons.org/licenses/by-nc-sa/4.0/)

You are free to share and adapt this project for non-commercial purposes, as long as you give appropriate credit and distribute derivatives under the same license.
