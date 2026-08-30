# GRControlSoftware — Software Architecture

**Project:** Gonioreflectometer Control System  
**Version:** 2.0 (ESP32-S3 hardware revision)  
**Date:** 2026-08-28

---

## 1. Overview

GRControlSoftware controls a tabletop **gonioreflectometer** — an optical instrument that photographs a material sample under systematically varied illumination and viewing angles. The system positions two motorised arms (a camera arm and an LED arc), fires individual LEDs at precise brightnesses, and triggers a scientific camera to build a multi-angle reflectance dataset.

The software is a three-layer stack:

```
┌──────────────────────────────────────────────────────┐
│            Browser UI  (Vanilla JS + HTML)            │
│       REST calls + WebSocket telemetry stream         │
├──────────────────────────────────────────────────────┤
│          Python Backend  (FastAPI / asyncio)          │
│    ESP32 connection · Camera driver · Scan engine     │
├──────────────────────────────────────────────────────┤
│           ESP32-S3 Firmware  (Arduino C++)            │
│   AccelStepper · AS5600 · OME85 BiSS-C · PCA9685     │
└──────────────────────────────────────────────────────┘
```

---

## 2. Repository Layout

```
GRControlSoftware/
├── run.py                     Startup script (uvicorn, reload)
├── requirements.txt
├── backend/
│   ├── main.py                FastAPI app, REST endpoints, WebSocket hub
│   ├── models.py              Pydantic models + Settings
│   ├── esp32/
│   │   ├── connection.py      Transport abstraction + ESP32Connection manager
│   │   └── protocol.py        JSON encode/decode + command builders
│   ├── camera/
│   │   └── ids_peak.py        IDS Peak SDK wrapper (lazy import)
│   └── scan/
│       └── controller.py      Scan state machine (asyncio task)
├── frontend/
│   ├── index.html             Single-page application shell
│   ├── js/
│   │   ├── app.js             All UI wiring and event handlers
│   │   ├── api.js             REST wrappers
│   │   ├── ws.js              Auto-reconnecting WebSocket dispatcher
│   │   └── log.js             In-browser event log
│   └── css/
│       └── style.css
├── ArduinoCode/
│   └── GRControl-Full/
│       └── GRControl-Full.ino ESP32-S3 firmware
└── docs/
    ├── esp32_protocol.md      Protocol specification
    └── software_architecture.md  (this file)
```

---

## 3. Hardware Map

| Hardware | Role | Interface |
|---|---|---|
| ESP32-S3 | Motion controller, LED driver | USB-Serial or WiFi TCP |
| PoStep60 #1 (Motor 1) | Camera arm stepper driver | STEP/DIR/EN — GPIO 21/47/48 |
| PoStep60 #2 (Motor 2) | LED arc stepper driver | STEP/DIR/EN — GPIO 38/2/1 |
| AS5600 #1 | Motor 1 shaft encoder (camera motor) | I2C0 SDA=10 SCL=11 |
| AS5600 #2 | Motor 2 shaft encoder (LED motor) | Software I2C SDA=7 SCL=15 |
| AS5600 #3 | LED arc rod output encoder (final position) | I2C1 SDA=4 SCL=5 |
| OME85 (Leine & Linde) | Camera arm output encoder (final position) | BiSS-C CLK=18 DATA=17 |
| PCA9685 | 7-channel PWM LED driver | Software I2C SDA=7 SCL=15 (0x40) |
| IDS Camera | Scientific image capture | USB (IDS Peak SDK) |

### Motor Gear Ratios and Step Conversion

| Motor | Output ratio | Microstep | Steps/output revolution | Conversion formula |
|---|---|---|---|---|
| Motor 1 — Camera | 8:1 (14→112T) | 1/16 (3200 steps/rev) | 25,600 | `steps = degrees × 8.0 × 3200 / 360` |
| Motor 2 — LED Arc | 3:1 (20→60T) | 1/16 (3200 steps/rev) | 9,600 | `steps = degrees × 3.0 × 3200 / 360` |

Maximum speed: 200 RPM motor shaft = **10,667 steps/sec**.

---

## 4. Firmware Architecture (ESP32-S3)

**File:** `ArduinoCode/GRControl-Full/GRControl-Full.ino`

### 4.1 Subsystems

```
┌─────────────────────────────────────────────────────────────┐
│                        Arduino loop()                        │
│   motor1.run()   motor2.run()   parseTelemetry()            │
│              handleSerial()  handleTCP()                     │
└────────────────────┬────────────────────────────────────────┘
                     │  dispatches to
        ┌────────────┼────────────────┐
        ▼            ▼                ▼
  handleMove()   handleJog()    handleLedSet()
  handleStop()   handleHome()   handleSetConfig()
        │
        ▼
  AccelStepper (motor1, motor2)
        │
        ▼
  PoStep60 driver chips (STEP/DIR pulses)
```

### 4.2 Encoder Subsystem

Three separate I2C buses are used because all three AS5600 sensors share the same I2C address (0x36):

```
Wire  (I2C0, SDA=10, SCL=11)   ──►  AS5600 #1  (Motor 1 shaft — camera motor)
Wire1 (I2C1, SDA=4,  SCL=5)    ──►  AS5600 #3  (LED arc rod — output encoder)
SoftI2C (SDA=7, SCL=15)        ──►  AS5600 #2  (Motor 2 shaft — LED motor)
                                ──►  PCA9685 @ 0x40
```

**AS5600 read:** 12-bit raw angle from registers `0x0C/0x0D`; converted to degrees with `raw × 360.0 / 4096.0`; home offset subtracted and result wrapped to `[0°, 360°)`.

**OME85 BiSS-C read:** Open-drain bit-bang using `INPUT_PULLUP` (high) and `OUTPUT+LOW` (low). 17-bit frame is read with interrupts disabled (`noInterrupts()`) to prevent timing jitter. The stuck MSB is stripped, leaving 16 bits (0–65535) that map to 0–360°. A 6-bit CRC with polynomial 0x43 validates each frame. Total read time ≈ 52 µs at 1 µs half-clocks.

### 4.3 LED Driver (PCA9685)

No Adafruit library is used because `Adafruit_PWMServoDriver` requires a hardware `TwoWire&` reference and both hardware buses are occupied by encoders. A minimal custom driver performs PCA9685 initialisation over software I2C:

1. `MODE1 = 0x10` (set SLEEP bit to allow prescaler write)
2. `PRESCALE = 5` → 1 kHz PWM frequency
3. `MODE1 = 0x00` (clear SLEEP)
4. `MODE1 = 0xA0` (set RESTART + Auto-Increment)

Per-channel writes use 4-byte auto-increment bursts to the `LED_ON_L` register group. `val=0` sets the full-off bit; `val≥4096` sets the full-on bit; otherwise PWM duty cycle.

### 4.4 NVS (Non-Volatile Storage)

Namespace `"grctrl"`, keys:

| Key | Type | Description |
|---|---|---|
| `homeCamera` | float | Camera arm home offset (degrees) |
| `homeLed` | float | LED arc home offset (degrees) |
| `dirFlip1` | bool | Invert Motor 1 direction |
| `dirFlip2` | bool | Invert Motor 2 direction |
| `maxSpeed` | float | Maximum stepper speed (SPS) |

NVS is loaded at boot. Direction flags are applied immediately via `AccelStepper::setPinsInverted(dirInvert, false, false)`, which inverts the DIR pin without affecting the internal step counter sign.

### 4.5 Boot Sequence

```
1. Serial.begin(115200)
2. WiFi AP / station setup → TCP server on port 8888
3. Wire.begin(10, 11)   — I2C0 for AS5600 #1
4. Wire1.begin(4, 5)    — I2C1 for AS5600 #3
5. pca_begin() → set prescale → wake PCA9685 → allLedsOff()
6. OME85 GPIO init (CLK/DATA as INPUT_PULLUP)
7. nvs_load() → setPinsInverted + setMaxSpeed on both steppers
8. initStepCounters()
     motor1.setCurrentPosition( cameraAngleToSteps( ome85DegFromHome() ) )
     motor2.setCurrentPosition( ledAngleToSteps( encoderDeg(Wire1, homeOffsetLed) ) )
```

After `initStepCounters()`, the stepper counters are synchronised to the absolute encoder positions, so the system knows its physical position immediately after power-on without a homing move.

### 4.6 Communication

The firmware accepts connections on both interfaces simultaneously:

- **Serial:** `Serial.read()` / `Serial.print()` at 115200 baud
- **WiFi TCP:** `WiFiServer` on port 8888; one accepted `WiFiClient` at a time

Both paths feed the same `parseMessage(String line)` dispatcher. Commands are JSON objects, one per line (`\n`-terminated). The firmware sends periodic `STATE` telemetry at the configured interval (default 100 ms) and sends `MOVE_DONE` when a move command completes.

---

## 5. Backend Architecture (Python / FastAPI)

**Entry point:** `run.py` → `uvicorn backend.main:app --reload`

### 5.1 Application Structure

```
main.py
  │
  ├── singletons (module-level)
  │     esp32        : ESP32Connection
  │     camera       : IDSCamera
  │     scan_ctrl    : ScanController
  │     device_state : DeviceState
  │     _ws_clients  : set[WebSocket]
  │
  ├── FastAPI routes (REST + WS)
  │
  ├── _on_esp32_message()  ← registered as ESP32 callback
  │     updates device_state
  │     broadcasts over /ws
  │     forwards to scan_ctrl.on_esp32_message()
  │
  └── scan callbacks
        _on_scan_progress() → broadcast "scan_progress"
        _on_scan_image()    → broadcast "image_captured" (base64 JPEG)
```

### 5.2 ESP32 Connection Layer (`backend/esp32/`)

#### Transport abstraction

```
_Transport (ABC)
├── SerialTransport   — pyserial-asyncio, 115200 baud
└── TcpTransport      — asyncio.open_connection, port 8888
```

Both implement `connect()`, `disconnect()`, `write(data)`, and `read_lines()` (async generator). The identical interface means the rest of the code is transport-agnostic.

#### ESP32Connection

Wraps a `_Transport` and provides:

- `connect_serial(port)` / `connect_tcp(host)` — selects transport, calls `_do_connect()`
- `send_raw(data)` — fire-and-forget write (used by all current routes)
- `send_command(data, expect_ack_for)` — write + await ACK future (infrastructure exists but unused; all routes use `send_raw`)
- `_read_loop()` — asyncio task; reads lines, JSON-decodes, dispatches to callbacks
- `_ping_loop()` — asyncio task; sends `PING` every `settings.ping_interval_s`
- `add_callback(cb)` / `remove_callback(cb)` — registers async message callbacks

On connect, `SET_TELEMETRY` is sent immediately to establish the preferred telemetry rate.

#### Protocol (`backend/esp32/protocol.py`)

All messages are JSON objects terminated with `\n`. Two functions handle encoding/decoding:

```python
encode(msg: dict) -> bytes   # json.dumps + "\n" + utf-8
decode(line: str) -> dict    # json.loads after strip
```

Command builders (one per protocol message type):

| Function | ESP32 command |
|---|---|
| `cmd_ping()` | `PING` |
| `cmd_get_state()` | `GET_STATE` |
| `cmd_move(motor, angle, speed)` | `MOVE` |
| `cmd_jog(motor, direction, degrees, speed)` | `JOG` |
| `cmd_stop(motor)` | `STOP` |
| `cmd_set_home(motor)` | `SET_HOME` |
| `cmd_led_set(index, state, brightness)` | `LED_SET` |
| `cmd_led_all(states, brightness)` | `LED_ALL` |
| `cmd_led_off_all()` | `LED_OFF_ALL` |
| `cmd_set_telemetry(interval_ms)` | `SET_TELEMETRY` |
| `cmd_set_config(dir_flip_1, dir_flip_2, max_speed_sps)` | `SET_CONFIG` |

### 5.3 Data Models (`backend/models.py`)

```
Settings (pydantic-settings, env prefix GR_)
  serial_baudrate       int    = 115200
  esp32_tcp_port        int    = 8888
  telemetry_interval_ms int    = 100
  ping_interval_s       float  = 5.0
  command_timeout_s     float  = 2.0

Motor (IntEnum)
  BOTH    = 0
  CAMERA  = 1   ← Motor 1, camera arm
  LED_ARC = 2   ← Motor 2, LED arc

EncoderState (BaseModel)
  motor1_deg      float         AS5600 #1, Motor 1 shaft
  motor2_deg      float         AS5600 #2, Motor 2 shaft
  led_arc_deg     Optional[float]  AS5600 #3, LED arc output
  camera_deg      Optional[float]  OME85, camera arm output
  cmd_camera_deg  Optional[float]  commanded output position for camera
  cmd_led_deg     Optional[float]  commanded output position for LED
  ── computed properties ──
  led_error_deg     led_arc_deg − cmd_led_deg,     normalised to [−180, 180]
  camera_error_deg  camera_deg  − cmd_camera_deg,  normalised to [−180, 180]

DeviceState (BaseModel)
  connected        bool
  connection_type  Optional[str]
  encoder          EncoderState
  motor1_moving    bool
  motor2_moving    bool
  led_states       list[int]       7 elements, 0/1
  led_brightness   list[int]       7 elements, 0–255
  ts               int             ESP32 boot ms
  dir_flip_1       bool
  dir_flip_2       bool

ScanConfig (BaseModel)
  led_axis         AxisConfig (start, stop, step, speed_pct)
  camera_axis      AxisConfig
  led_pattern      LedPattern (enabled[7], brightness[7])
  move_simultaneously  bool
  output_folder    str
  image_format     str

ScanProgress (BaseModel)
  running          bool
  paused           bool
  total_positions  int
  current_position int
  images_captured  int
  errors           list[str]
  current_led_pos_deg   float
  current_cam_pos_deg   float
  current_led_index     int
```

### 5.4 Scan Controller (`backend/scan/controller.py`)

An asyncio-based state machine that runs the full measurement scan as a background task.

**State:** `ScanProgress` object updated throughout execution.

**Motion synchronisation:** Two `asyncio.Event` objects (`_move_done_motor1`, `_move_done_motor2`) are set by `on_esp32_message()` when `MOVE_DONE` arrives for each motor. The `_move_to()` coroutine clears them before issuing moves and then `await`s both with a 30-second timeout.

**Scan loop (pseudocode):**

```
for led_pos in led_axis.positions:
  for cam_pos in camera_axis.positions:
    while paused: sleep(0.1)
    _move_to(led_pos, cam_pos)   # await MOVE_DONE for both motors
    for led_idx in active_leds:
      while paused: sleep(0.1)
      LED on
      sleep(50 ms settle)
      camera.capture(filename, format, metadata)
      LED off
      emit progress + image preview
```

**File naming:** `LED_<deg>deg_CAM_<deg>deg_LED<NN>_<timestamp>.<fmt>`

**Metadata sidecar:** JSON file alongside each image containing target angles, LED index/brightness, and all four encoder readings plus computed errors at capture time.

**Pause/abort:** `pause()` sets a flag and sends `STOP(0)`. `abort()` cancels the asyncio task, sends `STOP(0)` + `LED_OFF_ALL`, and resets progress.

### 5.5 Camera Driver (`backend/camera/ids_peak.py`)

Wraps the IDS Peak SDK (optional dependency). The SDK uses blocking calls which run in a `ThreadPoolExecutor` via `_run_sync()` — they are never called directly on the event loop thread.

Key methods (all `async`):

| Method | Description |
|---|---|
| `open()` | Open first available IDS camera |
| `close()` | Release camera and executor |
| `set_exposure(us)` | Set exposure time in microseconds |
| `set_gain(gain)` | Set analogue gain |
| `capture(filename, fmt, metadata)` | Capture + save image + JSON sidecar |
| `grab_preview_jpeg()` | Grab a JPEG frame for live preview |
| `get_info()` | Return model, serial, current settings |

If the IDS Peak SDK is not installed, `import ids_peak` raises `ImportError` at first use (not at import time), allowing the backend to start and serve the motor/scan UI without a camera connected.

### 5.6 REST API

Base URL: `http://localhost:8000`

| Method | Path | Description |
|---|---|---|
| GET | `/` | Serve `frontend/index.html` |
| GET | `/static/*` | Serve frontend assets |
| GET | `/api/ports` | List available serial ports |
| POST | `/api/connect` | Connect to ESP32 (serial or TCP) |
| POST | `/api/disconnect` | Disconnect |
| POST | `/api/motor/move` | Move motor to absolute angle |
| POST | `/api/motor/jog` | Jog motor by relative degrees |
| POST | `/api/motor/stop` | Stop motor |
| POST | `/api/motor/home` | Set current position as home |
| POST | `/api/motor/config` | Set direction flip and/or max speed |
| POST | `/api/led/set` | Set single LED state + brightness |
| POST | `/api/led/all` | Set all 7 LEDs at once |
| POST | `/api/led/off` | Turn off all LEDs |
| GET | `/api/camera/info` | Camera model, serial, settings |
| POST | `/api/camera/open` | Open camera |
| POST | `/api/camera/close` | Close camera |
| POST | `/api/camera/settings` | Set exposure and gain |
| GET | `/api/camera/preview` | Single JPEG frame |
| GET | `/api/camera/stream` | MJPEG live stream (multipart) |
| POST | `/api/scan/start` | Start scan with `ScanConfig` body |
| POST | `/api/scan/pause` | Pause running scan |
| POST | `/api/scan/resume` | Resume paused scan |
| POST | `/api/scan/abort` | Abort scan |
| GET | `/api/scan/status` | Current `ScanProgress` |
| WS | `/ws` | Real-time event stream |

### 5.7 WebSocket Hub

`/ws` is a persistent bidirectional channel. The frontend sends nothing over it (commands go via REST). The backend broadcasts JSON objects with an `event` field:

| event | Trigger | Key payload fields |
|---|---|---|
| `connected_to_backend` | WS open | `device_connected`, `connection_type` |
| `state` | Each ESP32 STATE message | All encoder readings, motor moving flags, LED states, dir_flip |
| `move_done` | ESP32 MOVE_DONE | `motor`, `final_angle` |
| `error` | ESP32 ERROR | `code`, `msg` |
| `esp32_connected` | POST /api/connect success | `mode` |
| `disconnected` | Disconnect or transport loss | — |
| `camera_opened` | POST /api/camera/open | `info` |
| `camera_closed` | POST /api/camera/close | — |
| `scan_progress` | Each scan step | Full `ScanProgress` dump |
| `image_captured` | After each capture | `path`, `preview_b64` (JPEG) |

---

## 6. Frontend Architecture (Vanilla JS)

No build tool, no transpilation. The page is a single HTML file loaded as a static asset. JS modules are loaded with `<script type="module">`.

### 6.1 Module Responsibilities

| File | Responsibility |
|---|---|
| `app.js` | DOM queries, event listeners, WebSocket handler registration, UI state management |
| `api.js` | `fetch()` wrappers for every REST endpoint; each exported function maps 1:1 to an endpoint |
| `ws.js` | Auto-reconnecting WebSocket client; event dispatcher pattern |
| `log.js` | In-browser scrolling event log |

### 6.2 WebSocket Client (`ws.js`)

```javascript
on("state", fn)       // register handler for an event type
on("*", fn)           // catch-all handler
onStatusChange(fn)    // connection status notifications
```

Dispatches on `msg.event`. Reconnects automatically after 3000 ms on close. Uses `wss://` when served over HTTPS, `ws://` otherwise.

### 6.3 App Module (`app.js`)

Registers one handler per WebSocket event type:

- **`state`** — updates all encoder readouts, error values, moving badges, LED state UI, direction flip checkboxes
- **`move_done`** — clears moving badge for the relevant motor
- **`scan_progress`** — updates progress bar, position counters, current angle display, scan button visibility
- **`image_captured`** — appends thumbnail to gallery grid
- **`esp32_connected` / `disconnected`** — updates connection pill colour and label
- **`camera_opened` / `camera_closed`** — updates camera pill

All user actions call `api.js` functions and log to `log.js`.

### 6.4 UI Layout

```
┌─ topbar ──────────────────────────────────────────────────────┐
│ GRControl  ● Disconnected  ● Camera off  [🌙] [⬛ E-STOP]    │
├─ sidebar ──────────────┬─ main ──────────────────────────────┐│
│ ▾ Connection           │ Encoder Readings (2-col grid)       ││
│   [Serial | WiFi/TCP]  │   Camera axis │ LED axis            ││
│                        │   Motor shaft │ Motor shaft         ││
│ ▾ Reference / Home     │   Output enc  │ Output enc          ││
│   [Home Both]          │   Error       │ Error               ││
│   [Home LED] [Home Cam]│                                     ││
│                        │ Camera Control                      ││
│ ▾ Log                  │   settings + live MJPEG preview     ││
│   [event log...]       │                                     ││
│                        │ Scan Configuration                  ││
│                        │   LED arc axis + camera axis        ││
│                        │   LED pattern (7 checkboxes)        ││
│                        │   [▶ Start] [⏸ Pause] [⬛ Abort]   ││
│                        │                                     ││
│                        │ Scan Progress (shown while running) ││
│                        │                                     ││
│                        │ Captured Images (gallery)           ││
│                        │                                     ││
│                        │ Manual Motor Control (2-col)        ││
│                        │   Motor 1 Camera │ Motor 2 LED Arc  ││
│                        │   jog/move/stop/dir-flip            ││
│                        │                                     ││
│                        │ LED Control (7 sliders)             ││
└────────────────────────┴─────────────────────────────────────┘┘
```

---

## 7. Data Flow

### 7.1 Command Path (UI → Hardware)

```
User clicks button
  → app.js calls api.js function
  → fetch() POST to FastAPI route
  → route calls esp32.send_raw(cmd_*())
  → _Transport.write() sends JSON line to ESP32
  → ESP32 firmware parses JSON and executes
```

All commands are fire-and-forget (`send_raw`). The ACK machinery in `connection.py` (`send_command` + `_pending` futures) exists but is not used by any route.

### 7.2 Telemetry Path (Hardware → UI)

```
ESP32 firmware
  → sends STATE JSON every 100 ms over serial/TCP
  → ESP32Connection._read_loop() reads line
  → decode(line) → dict
  → _dispatch(msg)
      → resolves any pending ACK futures (for ACK/PONG types)
      → calls all registered callbacks
          → main.py:_on_esp32_message(msg)
               → updates device_state
               → _broadcast({"event": "state", ...})
                    → WebSocket.send_json() to all _ws_clients
          → scan_ctrl.on_esp32_message(msg)
               → updates current_encoder
               → sets _move_done_motor1/2 events on MOVE_DONE

Browser WebSocket
  → ws.js receives message
  → dispatch(msg.event, msg)
  → app.js "state" handler updates DOM
```

### 7.3 Scan Data Path

```
scan_ctrl._run_scan() (asyncio task)
  → for each (led_pos × cam_pos × led_idx):
      esp32.send_raw(cmd_move(...))  ×2
      await _move_done_motor1 + _move_done_motor2 events
      esp32.send_raw(cmd_led_set(on))
      camera.capture(filename, format, metadata)   ← ThreadPoolExecutor
      esp32.send_raw(cmd_led_set(off))
      camera.grab_preview_jpeg()                   ← ThreadPoolExecutor
      _on_scan_image(path, jpeg)
        → _broadcast("image_captured", preview_b64)
      _on_scan_progress(progress)
        → _broadcast("scan_progress", ...)
```

---

## 8. ESP32 Protocol Reference

All messages: UTF-8 JSON, one object per line, `\n`-terminated. Identical over Serial and TCP.

### PC → ESP32

```json
{"type": "PING"}
{"type": "GET_STATE"}
{"type": "MOVE",        "motor": 1, "angle": 45.0, "speed": 80}
{"type": "JOG",         "motor": 1, "direction": 1, "degrees": 5.0, "speed": 30}
{"type": "STOP",        "motor": 0}
{"type": "SET_HOME",    "motor": 0}
{"type": "LED_SET",     "index": 2, "state": 1, "brightness": 200}
{"type": "LED_ALL",     "states": [0,0,1,0,0,0,0], "brightness": [0,0,200,0,0,0,0]}
{"type": "LED_OFF_ALL"}
{"type": "SET_TELEMETRY","interval_ms": 100}
{"type": "SET_CONFIG",  "dir_flip_1": true, "dir_flip_2": false, "max_speed_sps": 10667}
```

`motor` values: `1` = Camera arm, `2` = LED arc, `0` = both.  
`direction` values: `1` = CW (positive), `-1` = CCW (negative).

### ESP32 → PC

```json
{"type": "STATE",    "ts": 123456,
 "enc_motor1_deg": 10.0, "enc_motor2_deg": 9.99,
 "enc_led_arc_deg": 10.45, "enc_camera_deg": 10.01,
 "cmd_camera_deg": 10.0, "cmd_led_deg": 10.0,
 "motor1_moving": false, "motor2_moving": false,
 "led_states": [0,0,1,0,0,0,0], "led_brightness": [0,0,200,0,0,0,0],
 "dir_flip_1": false, "dir_flip_2": false}

{"type": "ACK",       "cmd": "MOVE", "ts": 123460}
{"type": "NACK",      "cmd": "MOVE", "reason": "Motor already moving", "ts": 123461}
{"type": "PONG",      "ts": 123462}
{"type": "MOVE_DONE", "motor": 1, "final_angle": 45.0023, "ts": 123999}
{"type": "ERROR",     "code": 3, "msg": "I2C timeout", "ts": 124000}
```

---

## 9. Key Design Decisions

### 9.1 Dual Transport (Serial + TCP)

The same JSON protocol runs over both USB-Serial and WiFi TCP. This was a deliberate design choice: during development and bench use, Serial is more reliable and easier to debug; during an experiment on an optical bench where cabling would interfere, WiFi TCP allows cable-free operation. The `_Transport` ABC makes the choice invisible to the rest of the backend.

### 9.2 Boot-Time Position Initialisation (No Re-Homing)

The step counters on both motors are initialised from absolute encoders at boot:
- Camera arm: OME85 BiSS-C (16-bit absolute, 0–65535 → 0–360°)
- LED arc: AS5600 #3 on the output rod (12-bit absolute, 0–4095 → 0–360°)

This means the system knows its physical position immediately after power-on. No homing move is required. Home offsets are stored in NVS and subtracted at read time.

### 9.3 Error Metric Definition

The tracking error is defined as:

```
error = output_encoder_degrees − commanded_output_degrees
```

`commanded_output_degrees` is derived from the stepper's internal position counter (converted back to output-shaft degrees via the gear ratio). This is meaningful as an open-loop tracking error — it shows how far the physical output deviates from where the controller *thinks* it commanded. It is computed as a computed property on `EncoderState` in the backend (not on the firmware), normalised to `(−180°, 180°]`.

An earlier design compared output encoder to motor shaft encoder, which is dimensionally inconsistent across different gear ratios and does not represent a useful metric.

### 9.4 Software I2C for PCA9685

Both hardware I2C buses (Wire, Wire1) are occupied by AS5600 encoders. AS5600 #2 (Motor 2 shaft) and the PCA9685 share a software I2C bus on GPIO 7/15. The software I2C uses open-drain bit-bang: `INPUT_PULLUP` mode for logic high, `OUTPUT + LOW` for logic low. Half-bit period is 5 µs ≈ 100 kHz. No Adafruit PWM library is used — a minimal custom PCA9685 driver was written that uses this same bit-bang bus directly.

### 9.5 Fire-and-Forget Commands

All REST routes call `esp32.send_raw()` (fire-and-forget) rather than `send_command()` (await ACK). The `send_command` / ACK-future machinery exists in `connection.py` but is unused. The protocol spec (§6.4) requires retry on missing ACK, but this is not implemented on the PC side. In practice the serial/TCP link is reliable at short distances and the telemetry stream confirms motion status.

### 9.6 Camera Thread Isolation

The IDS Peak SDK is entirely synchronous and blocking. All SDK calls execute in a `ThreadPoolExecutor` via `asyncio.get_event_loop().run_in_executor()` (wrapped as `_run_sync` inside `ids_peak.py`). This prevents any SDK call from blocking the FastAPI event loop. The live MJPEG stream uses an `asyncio.Lock` to prevent concurrent camera access between the stream generator and the scan controller.

### 9.7 Frontend: No Build Toolchain

The frontend is intentionally kept as plain HTML/CSS/JS ES modules with no transpilation, bundling, or framework. This keeps the project self-contained (no `node_modules`, no build step), makes the frontend directly editable and immediately deployable as static files served by FastAPI, and eliminates a class of toolchain-related fragility. The trade-off is no TypeScript, no tree-shaking, and manual module imports.

---

## 10. Settings and Configuration

All backend settings are managed by `Settings` (pydantic-settings) with environment variable overrides using the `GR_` prefix:

| Setting | Default | Env var |
|---|---|---|
| `serial_baudrate` | 115200 | `GR_SERIAL_BAUDRATE` |
| `esp32_tcp_port` | 8888 | `GR_ESP32_TCP_PORT` |
| `telemetry_interval_ms` | 100 | `GR_TELEMETRY_INTERVAL_MS` |
| `ping_interval_s` | 5.0 | `GR_PING_INTERVAL_S` |
| `command_timeout_s` | 2.0 | `GR_COMMAND_TIMEOUT_S` |

---

## 11. Known Limitations

- **No ACK retry:** `send_raw` is fire-and-forget. If the ESP32 NACKs a command (e.g., move while already moving) the backend does not detect or retry it.
- **Single live stream consumer:** `GET /api/camera/stream` is locked to one consumer at a time. A second browser tab requesting the stream receives HTTP 409.
- **Scan status via private field:** `GET /api/scan/status` reads `scan_ctrl._progress` directly rather than through a public accessor.
- **`remove_callback` silent no-op:** `connection.py` calls `.discard()` on a `list` (which has no such method); the `hasattr` guard makes this a no-op. The subsequent `.remove()` call works correctly.
- **No test suite:** There are no automated tests. Correctness is verified by hardware-in-the-loop testing.
