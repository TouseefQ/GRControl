# CLAUDE.md

Guidance for working in this repository.

## What this is

**GRControl** — browser-based control software for a tabletop **gonioreflectometer**
(an optical instrument that photographs a sample under controlled illumination and
viewing angles). A FastAPI backend drives an **ESP32** (motors, LEDs, encoders) over
serial or TCP, and an **IDS camera** over USB. A no-build vanilla-JS frontend talks to
the backend via REST + a WebSocket.

The repo is small (~21 source files). Everything under `.venv/` and `__pycache__/` is
gitignored — ignore it.

## Run / setup

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python run.py            # serves http://localhost:8000 (uvicorn, reload on backend/)
```

There is no test suite, linter config, or build step. The frontend is served as static
files — no transpilation. `run.py` forces `WindowsSelectorEventLoopPolicy` because
pyserial-asyncio needs it on Windows.

Python 3.9+. The IDS Peak SDK is **optional** — the camera module imports it lazily so
the backend runs (motors/LEDs/scan-config/UI) without a camera attached.

## Architecture

```
backend/                  Python / FastAPI
  main.py                 FastAPI app: REST endpoints, /ws WebSocket hub,
                          central ESP32->frontend message router; holds singletons
                          (esp32, camera, scan_ctrl, device_state, _ws_clients)
  models.py               Pydantic models + Settings (env prefix GR_)
  esp32/
    connection.py         _Transport ABC -> SerialTransport / TcpTransport;
                          read loop, ACK-future map, ping/watchdog loop
    protocol.py           JSON-newline encode/decode + cmd_* builders
  camera/ids_peak.py      IDS Peak SDK wrapper; blocking calls run in a ThreadPoolExecutor
  scan/controller.py      Scan state machine (asyncio task)
frontend/                 Vanilla JS + HTML/CSS, no build
  index.html
  js/app.js               All UI wiring
  js/api.js               REST wrappers
  js/ws.js                Auto-reconnecting WebSocket dispatcher (on(event, fn))
  js/log.js               In-browser event log
docs/esp32_protocol.md    Firmware-facing protocol spec (NOT firmware — that's user-written)
run.py                    Startup script
```

### Data flow
- **Commands** (UI -> hardware): frontend `api.js` -> FastAPI REST route in `main.py` ->
  `esp32.send_raw(cmd_*())` -> ESP32. Fire-and-forget.
- **Telemetry** (hardware -> UI): ESP32 -> `connection.py` read loop -> `_dispatch` ->
  callbacks. `main.py:_on_esp32_message` updates `device_state` and broadcasts an
  `event`-tagged JSON over `/ws`; `scan_ctrl.on_esp32_message` consumes `MOVE_DONE`/`STATE`.
- The frontend `ws.js` dispatches on `msg.event`; handlers register via `on("state", ...)` etc.

### ESP32 protocol (see docs/esp32_protocol.md)
JSON objects, one per line (`\n`-terminated), identical over serial and TCP. PC is master.
- PC->ESP32: `PING`, `GET_STATE`, `MOVE`, `JOG`, `STOP`, `SET_HOME`, `LED_SET`, `LED_ALL`,
  `LED_OFF_ALL`, `SET_TELEMETRY`.
- ESP32->PC: `STATE` (periodic telemetry, default 100ms), `ACK`/`NACK`, `PONG`,
  `MOVE_DONE`, `ERROR`.
- Motors: `1` = LED arc, `2` = camera, `0` = both. 7 LEDs (index 0–6), brightness 0–255.

### Scan sequence (scan/controller.py)
For each (LED angle x camera angle): move both motors (simultaneous or sequential per
`move_simultaneously`), wait for both `MOVE_DONE`, then for each enabled LED ->
on / capture image + JSON sidecar / off. Emits `scan_progress` and `image_captured`
(base64 JPEG preview) over the WebSocket. Supports pause/resume/abort.

Captured files are named
`LED_<deg>deg_CAM_<deg>deg_LED<NN>_<timestamp>.<fmt>` with a matching `.json` sidecar of
encoder readings + metadata.

## Conventions
- Angles are floats in degrees, rounded to 4 dp. Encoder **error** = output-shaft encoder
  minus motor-shaft encoder, normalized to [-180, 180] (`models.py` EncoderState properties).
- All blocking IDS SDK calls go through `_run_sync` / the module `_executor` — never call
  them directly on the event loop.
- Settings come from `Settings` (pydantic-settings), overridable via `GR_`-prefixed env vars.

## Known rough edges (verify before relying on these)
- `connection.py` `remove_callback` calls `.discard` on a `list` (no such method); guarded
  by `hasattr` so it's a silent no-op before the real `list.remove`. Dead code.
- `main.py` `/api/scan/status` reads the private `scan_ctrl._progress`.
- `send_command` / the ACK-future machinery in `connection.py` exists but is unused — every
  route uses fire-and-forget `send_raw`, so the protocol's "retry on missing ACK" rule
  (spec §6.4) is not implemented on the PC side.
