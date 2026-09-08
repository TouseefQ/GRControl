"""
FastAPI application — serves the frontend and exposes the REST + WebSocket API.

Endpoints:
  GET  /                          → frontend index.html
  GET  /static/*                  → frontend assets
  GET  /api/ports                 → list serial ports
  POST /api/connect               → connect to ESP32 (serial or TCP)
  POST /api/disconnect            → disconnect
  POST /api/motor/move            → move motor to absolute angle
  POST /api/motor/jog             → jog motor
  POST /api/motor/stop            → stop motor
  POST /api/motor/home            → set home
  POST /api/led/set               → set single LED
  POST /api/led/all               → set all LEDs
  POST /api/led/off               → turn off all LEDs
  GET  /api/camera/info           → camera info
  POST /api/camera/open           → open camera
  POST /api/camera/close          → close camera
  POST /api/camera/settings       → set exposure/gain
  POST /api/camera/capture        → save one frame to disk on demand
  POST /api/scan/start            → start a scan run
  POST /api/scan/pause            → pause
  POST /api/scan/resume           → resume
  POST /api/scan/abort            → abort
  GET  /api/scan/status           → current scan progress
  WS   /ws                        → real-time bidirectional channel
"""
import asyncio
import base64
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from .models import Settings, ScanConfig, DeviceState, EncoderState
from .esp32.connection import ESP32Connection, list_serial_ports
from .esp32.protocol import (
    cmd_move, cmd_jog, cmd_stop, cmd_set_home,
    cmd_led_set, cmd_led_all, cmd_led_off_all, cmd_set_config,
)
from .camera.ids_peak import IDSCamera
from .scan.controller import ScanController
from . import motion

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

settings = Settings()
app = FastAPI(title="GRControlSoftware")

# ── Singletons ────────────────────────────────────────────────────────────────
esp32 = ESP32Connection(settings)
camera = IDSCamera()
scan_ctrl = ScanController(esp32, camera, settings)
device_state = DeviceState()
_ws_clients: set[WebSocket] = set()

# ── Encoder jitter capture (diagnostics) ────────────────────────────────────────
# When active, every STATE telemetry frame appends the resting output-encoder
# readings here so /api/encoder/jitter can report their peak-to-peak scatter.
_jitter_active = False
_jitter_camera: list = []   # OME85 (camera final) readings
_jitter_led: list = []      # AS5600 #3 (LED arc final) readings

# ── Static files & SPA ────────────────────────────────────────────────────────
_frontend = Path(__file__).parent.parent / "frontend"
app.mount("/static", StaticFiles(directory=str(_frontend)), name="static")


@app.get("/")
async def index():
    return FileResponse(str(_frontend / "index.html"))


# ── WebSocket broadcast ───────────────────────────────────────────────────────

async def _broadcast(msg: dict):
    dead = set()
    for ws in _ws_clients:
        try:
            await ws.send_json(msg)
        except Exception:
            dead.add(ws)
    _ws_clients.difference_update(dead)


# ── ESP32 message handler ─────────────────────────────────────────────────────

async def _on_esp32_message(msg: dict):
    mtype = msg.get("type")

    if mtype == "STATE":
        device_state.encoder.motor1_deg = msg.get("enc_motor1_deg", 0.0)
        device_state.encoder.motor2_deg = msg.get("enc_motor2_deg", 0.0)
        device_state.encoder.led_arc_deg = msg.get("enc_led_arc_deg")
        device_state.encoder.camera_deg = msg.get("enc_camera_deg")
        device_state.encoder.cmd_camera_deg = msg.get("cmd_camera_deg")
        device_state.encoder.cmd_led_deg = msg.get("cmd_led_deg")
        device_state.motor1_moving = msg.get("motor1_moving", False)
        device_state.motor2_moving = msg.get("motor2_moving", False)
        device_state.led_states = msg.get("led_states", [0] * 7)
        device_state.led_brightness = msg.get("led_brightness", [0] * 7)
        device_state.ts = msg.get("ts", 0)
        device_state.dir_flip_1 = msg.get("dir_flip_1", False)
        device_state.dir_flip_2 = msg.get("dir_flip_2", False)

        if _jitter_active:
            _jitter_camera.append(msg.get("enc_camera_deg"))
            _jitter_led.append(msg.get("enc_led_arc_deg"))

        await _broadcast({
            "event": "state",
            "enc_motor1_deg": device_state.encoder.motor1_deg,
            "enc_motor2_deg": device_state.encoder.motor2_deg,
            "enc_led_arc_deg": device_state.encoder.led_arc_deg,
            "enc_camera_deg": device_state.encoder.camera_deg,
            "cmd_camera_deg": device_state.encoder.cmd_camera_deg,
            "cmd_led_deg": device_state.encoder.cmd_led_deg,
            "led_error_deg": device_state.encoder.led_error_deg,
            "camera_error_deg": device_state.encoder.camera_error_deg,
            "motor1_moving": device_state.motor1_moving,
            "motor2_moving": device_state.motor2_moving,
            "led_states": device_state.led_states,
            "led_brightness": device_state.led_brightness,
            "ts": device_state.ts,
            "dir_flip_1": device_state.dir_flip_1,
            "dir_flip_2": device_state.dir_flip_2,
        })

    elif mtype == "MOVE_DONE":
        await _broadcast({"event": "move_done", "motor": msg.get("motor"),
                          "final_angle": msg.get("final_angle")})

    elif mtype == "ERROR":
        await _broadcast({"event": "error", "code": msg.get("code"),
                          "msg": msg.get("msg")})

    elif mtype == "DISCONNECTED":
        device_state.connected = False
        device_state.connection_type = None
        await _broadcast({"event": "disconnected"})

    # Forward to scan controller for MOVE_DONE tracking
    await scan_ctrl.on_esp32_message(msg)


esp32.add_callback(_on_esp32_message)


# ── Scan progress callbacks ───────────────────────────────────────────────────

async def _on_scan_progress(progress):
    await _broadcast({"event": "scan_progress", **progress.model_dump()})


async def _on_scan_image(path: str, jpeg_bytes: bytes):
    b64 = base64.b64encode(jpeg_bytes).decode()
    await _broadcast({"event": "image_captured", "path": path, "preview_b64": b64})


scan_ctrl.set_progress_callback(_on_scan_progress)
scan_ctrl.set_image_callback(_on_scan_image)


# ── WebSocket endpoint ────────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    _ws_clients.add(ws)
    # Send current state immediately on connect
    await ws.send_json({
        "event": "connected_to_backend",
        "device_connected": device_state.connected,
        "connection_type": device_state.connection_type,
    })
    try:
        while True:
            await ws.receive_text()  # keep connection alive; client-to-server via REST
    except WebSocketDisconnect:
        _ws_clients.discard(ws)


# ── REST: Connection ──────────────────────────────────────────────────────────

@app.get("/api/ports")
async def get_ports():
    return {"ports": list_serial_ports()}


class ConnectRequest(BaseModel):
    mode: str  # "serial" | "tcp"
    port: Optional[str] = None
    host: Optional[str] = None


@app.post("/api/connect")
async def connect(req: ConnectRequest):
    try:
        if req.mode == "serial":
            if not req.port:
                raise HTTPException(400, "port required for serial mode")
            await esp32.connect_serial(req.port)
        elif req.mode == "tcp":
            if not req.host:
                raise HTTPException(400, "host required for tcp mode")
            await esp32.connect_tcp(req.host)
        else:
            raise HTTPException(400, "mode must be 'serial' or 'tcp'")
        device_state.connected = True
        device_state.connection_type = req.mode
        await _broadcast({"event": "esp32_connected", "mode": req.mode})
        return {"status": "connected", "mode": req.mode}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/disconnect")
async def disconnect():
    await esp32.disconnect()
    device_state.connected = False
    device_state.connection_type = None
    await _broadcast({"event": "disconnected"})
    return {"status": "disconnected"}


# ── REST: Motor control ───────────────────────────────────────────────────────

class MoveRequest(BaseModel):
    motor: int
    angle: float
    speed: int = 80
    precise: Optional[bool] = None  # None → fall back to settings.precise_enabled


class JogRequest(BaseModel):
    motor: int
    direction: int
    degrees: float
    speed: int = 30


class StopRequest(BaseModel):
    motor: int = 0


class HomeRequest(BaseModel):
    motor: int = 0


class ConfigRequest(BaseModel):
    dir_flip_1: Optional[bool] = None
    dir_flip_2: Optional[bool] = None
    max_speed_sps: Optional[float] = None


@app.post("/api/motor/move")
async def motor_move(req: MoveRequest):
    use_precise = req.precise if req.precise is not None else settings.precise_enabled
    if use_precise:
        result = await motion.move_precise(
            esp32, req.motor, req.angle, req.speed,
            **settings.precise_params_for(req.motor),
        )
        await _broadcast({
            "event": "move_result",
            "motor": req.motor,
            "target": req.angle,
            "achieved": result.get("achieved"),
            "residual": result.get("residual"),
            "iterations": result.get("iterations"),
            "converged": result.get("converged"),
            "encoder_ok": result.get("encoder_ok"),
        })
        return {"status": "ok", **result}
    await esp32.send_raw(cmd_move(req.motor, req.angle, req.speed))
    return {"status": "ok"}


@app.post("/api/motor/jog")
async def motor_jog(req: JogRequest):
    await esp32.send_raw(cmd_jog(req.motor, req.direction, req.degrees, req.speed))
    return {"status": "ok"}


@app.post("/api/motor/stop")
async def motor_stop(req: StopRequest):
    await esp32.send_raw(cmd_stop(req.motor))
    return {"status": "ok"}


@app.post("/api/motor/home")
async def motor_home(req: HomeRequest):
    await esp32.send_raw(cmd_set_home(req.motor))
    return {"status": "ok"}


@app.post("/api/motor/config")
async def motor_config(req: ConfigRequest):
    await esp32.send_raw(cmd_set_config(
        dir_flip_1=req.dir_flip_1,
        dir_flip_2=req.dir_flip_2,
        max_speed_sps=req.max_speed_sps,
    ))
    return {"status": "ok"}


class JitterRequest(BaseModel):
    duration_s: float = 10.0


@app.post("/api/encoder/jitter")
async def encoder_jitter(req: JitterRequest):
    """Diagnostics: hold the arm still, then call this to measure the resting
    scatter of the output encoders (OME85 camera / AS5600 #3 LED arc). Samples
    every STATE frame (~100 ms) for `duration_s`, then reports min/max/peak-to-
    peak/std-dev per encoder — the empirical noise floor to size the precise-
    positioning tolerances against. Do NOT move the motors while it runs."""
    global _jitter_active
    if not esp32.connected:
        raise HTTPException(409, "Not connected to ESP32")
    if _jitter_active:
        raise HTTPException(409, "Jitter capture already running")

    dur = max(1.0, min(req.duration_s, 120.0))
    _jitter_camera.clear()
    _jitter_led.clear()
    _jitter_active = True
    try:
        await asyncio.sleep(dur)
    finally:
        _jitter_active = False

    result = {
        "duration_s": dur,
        "samples": len(_jitter_camera),
        "camera_ome85": motion.jitter_stats(_jitter_camera),
        "led_arc_as5600": motion.jitter_stats(_jitter_led),
    }
    await _broadcast({"event": "encoder_jitter", **result})
    return result


# ── REST: LED control ─────────────────────────────────────────────────────────

class LedSetRequest(BaseModel):
    index: int
    state: int
    brightness: int


class LedAllRequest(BaseModel):
    states: list[int]
    brightness: list[int]


@app.post("/api/led/set")
async def led_set(req: LedSetRequest):
    await esp32.send_raw(cmd_led_set(req.index, req.state, req.brightness))
    return {"status": "ok"}


@app.post("/api/led/all")
async def led_all(req: LedAllRequest):
    await esp32.send_raw(cmd_led_all(req.states, req.brightness))
    return {"status": "ok"}


@app.post("/api/led/off")
async def led_off():
    await esp32.send_raw(cmd_led_off_all())
    return {"status": "ok"}


# ── REST: Camera ──────────────────────────────────────────────────────────────

class CameraSettingsRequest(BaseModel):
    exposure_us: Optional[float] = None
    gain: Optional[float] = None
    reverse_x: Optional[bool] = None  # True = mirror horizontally
    reverse_y: Optional[bool] = None  # True = flip vertically


@app.post("/api/camera/open")
async def camera_open():
    ok = await camera.open()
    if not ok:
        raise HTTPException(500, "Camera open failed — check IDS Peak SDK and connection")
    info = await camera.get_info()
    await _broadcast({"event": "camera_opened", "info": info})
    return {"status": "opened", "info": info}


@app.post("/api/camera/close")
async def camera_close():
    await camera.close()
    await _broadcast({"event": "camera_closed"})
    return {"status": "closed"}


@app.get("/api/camera/info")
async def camera_info():
    return await camera.get_info()


@app.post("/api/camera/settings")
async def camera_settings(req: CameraSettingsRequest):
    if req.exposure_us is not None:
        await camera.set_exposure(req.exposure_us)
    if req.gain is not None:
        await camera.set_gain(req.gain)
    if req.reverse_x is not None or req.reverse_y is not None:
        await camera.set_reverse(req.reverse_x, req.reverse_y)
    return {"status": "ok", "info": await camera.get_info()}


class CameraCaptureRequest(BaseModel):
    output_folder: str = "./captures"
    image_format: str = "tiff"


@app.post("/api/camera/capture")
async def camera_capture(req: CameraCaptureRequest):
    """Save a single frame to disk on demand (the UI 'Save Image' button).
    Works whether the preview is idle or a live MJPEG stream is running — it
    grabs its own frame from the camera. Saved RAW (no white balance) to the
    configured output folder in the selected format, with a JSON sidecar of
    the current encoder readings. Refuses while a scan owns the camera."""
    if not camera.is_open:
        raise HTTPException(503, "Camera not open")
    if scan_ctrl._progress.running:
        raise HTTPException(409, "Scan in progress — camera is busy")

    fmt = (req.image_format or "tiff").lower()
    if fmt not in ("tiff", "png", "jpeg", "jpg", "bmp"):
        fmt = "tiff"
    folder = req.output_folder.strip() or "./captures"
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
    path = f"{folder}/capture_{ts}.{fmt}"

    enc = device_state.encoder
    metadata = {
        "timestamp": datetime.now().isoformat(timespec="milliseconds"),
        "source": "manual_capture",
        "image_format": fmt,
        "enc_camera_deg": enc.camera_deg,
        "enc_led_arc_deg": enc.led_arc_deg,
        "enc_motor1_deg": enc.motor1_deg,
        "enc_motor2_deg": enc.motor2_deg,
    }
    saved = await camera.capture(path, fmt, metadata)
    if not saved:
        raise HTTPException(500, "Capture failed")

    # Push a thumbnail into the Captured Images gallery, same as scan captures.
    jpeg = await camera.grab_preview_jpeg()
    if jpeg:
        await _broadcast({"event": "image_captured", "path": saved,
                          "preview_b64": base64.b64encode(jpeg).decode()})
    return {"status": "saved", "path": saved}


@app.get("/api/camera/preview")
async def camera_preview():
    jpeg = await camera.grab_preview_jpeg()
    if not jpeg:
        raise HTTPException(503, "Preview not available")
    from fastapi.responses import Response
    return Response(content=jpeg, media_type="image/jpeg")


_preview_stream_lock = asyncio.Lock()


@app.get("/api/camera/stream")
async def camera_stream():
    """MJPEG live preview (multipart/x-mixed-replace) for a browser <img>.
    One consumer at a time; pauses while a scan runs so it never competes with
    scan capture for camera buffers. The stream ends when the camera closes or
    the client disconnects."""
    if not camera.is_open:
        raise HTTPException(503, "Camera not open")
    if _preview_stream_lock.locked():
        raise HTTPException(409, "Live preview already active")

    async def gen():
        loop = asyncio.get_event_loop()
        async with _preview_stream_lock:
            while camera.is_open:
                if scan_ctrl._progress.running:
                    await asyncio.sleep(0.2)  # yield the camera to the scan
                    continue
                t0 = loop.time()
                jpeg = await camera.grab_preview_jpeg()
                if not jpeg:
                    await asyncio.sleep(0.1)
                    continue
                yield (
                    b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                    + str(len(jpeg)).encode() + b"\r\n\r\n" + jpeg + b"\r\n"
                )
                # Cap at ~15 fps; if grab took longer, sleep(0) just yields the loop.
                await asyncio.sleep(max(0.0, 1 / 15 - (loop.time() - t0)))

    return StreamingResponse(
        gen(), media_type="multipart/x-mixed-replace; boundary=frame"
    )


# ── REST: Scan ────────────────────────────────────────────────────────────────

@app.post("/api/scan/start")
async def scan_start(config: ScanConfig):
    # Occlusion guard: positions where the LED arc would sit in front of the
    # lens (|θcam − θled| < keep-out) are skipped, not scanned — the controller
    # drops them and captures the rest. Only refuse outright if EVERY grid
    # position is occluded, since then there is nothing to capture.
    blocked = scan_ctrl.occluded_positions(config)
    total_pairs = len(config.led_axis.positions) * len(config.camera_axis.positions)
    if blocked and len(blocked) >= total_pairs:
        keepout = scan_ctrl.keepout_deg(config)
        raise HTTPException(
            422,
            f"All {total_pairs} scan position(s) fall within the {keepout:g}° "
            f"camera keep-out — the LED arc would block the lens's view of the "
            f"sample at every position. Widen the angle ranges, or lower the "
            f"keep-out angle (set to 0 to disable the guard).",
        )
    ok = await scan_ctrl.start(config)
    if not ok:
        raise HTTPException(409, "Scan already running")
    resp = {"status": "started"}
    if blocked:
        keepout = scan_ctrl.keepout_deg(config)
        sample = ", ".join(f"LED {l:g}°/CAM {c:g}°" for l, c in blocked[:5])
        more = "" if len(blocked) <= 5 else f" (+{len(blocked) - 5} more)"
        resp["skipped"] = len(blocked)
        resp["skipped_message"] = (
            f"{len(blocked)} position(s) skipped — within the {keepout:g}° camera "
            f"keep-out (LED arc blocks the lens): {sample}{more}"
        )
    return resp


@app.post("/api/scan/pause")
async def scan_pause():
    await scan_ctrl.pause()
    return {"status": "paused"}


@app.post("/api/scan/resume")
async def scan_resume():
    await scan_ctrl.resume()
    return {"status": "resumed"}


@app.post("/api/scan/abort")
async def scan_abort():
    await scan_ctrl.abort()
    return {"status": "aborted"}


@app.get("/api/scan/status")
async def scan_status():
    return scan_ctrl._progress.model_dump()
