/**
 * app.js — main entry point, wires up all UI modules
 */

import * as api from "./api.js";
import { log, logOk, logWarn, logErr } from "./log.js";
import { on, onStatusChange } from "./ws.js";

// ── Theme toggle ──────────────────────────────────────────────────────────────
const btnTheme = document.getElementById("btn-theme");
let _dark = true;

function applyTheme(dark) {
  _dark = dark;
  document.documentElement.classList.toggle("light", !dark);
  btnTheme.textContent = dark ? "🌙" : "☀️";
  btnTheme.title = dark ? "Switch to light theme" : "Switch to dark theme";
  localStorage.setItem("gr-theme", dark ? "dark" : "light");
}

// Restore saved preference
applyTheme(localStorage.getItem("gr-theme") !== "light");
btnTheme.addEventListener("click", () => applyTheme(!_dark));

// ── Sidebar collapse toggle ───────────────────────────────────────────────────
document.querySelectorAll(".sidebar-header").forEach(h => {
  h.addEventListener("click", () => {
    const target = document.getElementById(h.dataset.target);
    if (target) target.classList.toggle("collapsed");
  });
});

// ── Tab switching ─────────────────────────────────────────────────────────────
document.querySelectorAll(".tab").forEach(tab => {
  tab.addEventListener("click", () => {
    const parent = tab.closest(".sidebar-body") || tab.closest(".card");
    parent.querySelectorAll(".tab").forEach(t => t.classList.remove("active"));
    parent.querySelectorAll(".tab-panel").forEach(p => p.classList.remove("active"));
    tab.classList.add("active");
    document.getElementById(tab.dataset.tab)?.classList.add("active");
  });
});

// ── Connection status ─────────────────────────────────────────────────────────
const connLed   = document.getElementById("conn-led").querySelector("circle");
const connLabel = document.getElementById("conn-label");
const btnDisconn = document.getElementById("btn-disconnect");

function setLedColor(circleEl, color, glowColor) {
  circleEl.setAttribute("fill", color);
  circleEl.closest("svg").style.filter = glowColor
    ? `drop-shadow(0 0 3px ${glowColor})`
    : "none";
}

function setConnected(mode) {
  setLedColor(connLed, "#22c55e", "#22c55e");
  connLabel.textContent = `Connected (${mode})`;
  btnDisconn.classList.remove("hidden");
  document.getElementById("btn-connect-serial").textContent = "Reconnect";
  document.getElementById("btn-connect-tcp").textContent = "Reconnect";
}

function setDisconnected() {
  setLedColor(connLed, "#ef4444", null);
  connLabel.textContent = "Disconnected";
  btnDisconn.classList.add("hidden");
  document.getElementById("btn-connect-serial").textContent = "Connect";
  document.getElementById("btn-connect-tcp").textContent = "Connect";
}

// ── WebSocket status ──────────────────────────────────────────────────────────
onStatusChange(status => {
  if (status === "ws_closed") {
    log("WebSocket disconnected — retrying…", "warn");
  } else if (status === "ws_open") {
    log("Backend connected", "ok");
  }
});

on("connected_to_backend", (msg) => {
  if (msg.device_connected) setConnected(msg.connection_type);
});

on("esp32_connected", (msg) => {
  setConnected(msg.mode);
  logOk(`ESP32 connected via ${msg.mode}`);
});

on("disconnected", () => {
  setDisconnected();
  logWarn("ESP32 disconnected");
});

// ── Serial port refresh ───────────────────────────────────────────────────────
document.getElementById("btn-refresh-ports").addEventListener("click", async () => {
  const { ports } = await api.getPorts();
  const sel = document.getElementById("serial-port-select");
  sel.innerHTML = "";
  if (ports.length === 0) {
    sel.innerHTML = '<option value="">No ports found</option>';
  } else {
    ports.forEach(p => {
      const opt = document.createElement("option");
      opt.value = p.port;
      opt.textContent = `${p.port} — ${p.description}`;
      sel.appendChild(opt);
    });
  }
  log(`Found ${ports.length} serial port(s)`);
});

// ── Connect buttons ───────────────────────────────────────────────────────────
document.getElementById("btn-connect-serial").addEventListener("click", async () => {
  const port = document.getElementById("serial-port-select").value;
  if (!port) { logErr("Select a serial port first"); return; }
  try {
    await api.connectSerial(port);
    logOk(`Connecting to ${port}…`);
  } catch (e) { logErr(e.message); }
});

document.getElementById("btn-connect-tcp").addEventListener("click", async () => {
  const host = document.getElementById("tcp-host").value.trim();
  if (!host) { logErr("Enter an IP address"); return; }
  try {
    await api.connectTCP(host);
    logOk(`Connecting to ${host}…`);
  } catch (e) { logErr(e.message); }
});

btnDisconn.addEventListener("click", async () => {
  await api.disconnect();
  setDisconnected();
  logWarn("Disconnected from ESP32");
});

// ── E-STOP ────────────────────────────────────────────────────────────────────
document.getElementById("btn-estop").addEventListener("click", async () => {
  await api.motorStop(0);
  await api.ledOff();
  logErr("E-STOP triggered — all motors stopped, all LEDs off");
});

// ── Encoder display ───────────────────────────────────────────────────────────
function fmt(val) {
  return typeof val === "number" ? val.toFixed(4) : "—";
}

function errorClass(deg) {
  const a = Math.abs(deg);
  if (a < 0.5) return "ok";
  if (a < 2.0) return "warn";
  return "bad";
}

on("state", (msg) => {
  document.getElementById("enc-motor1").innerHTML = `${fmt(msg.enc_motor1_deg)}<span>°</span>`;
  document.getElementById("enc-motor2").innerHTML = `${fmt(msg.enc_motor2_deg)}<span>°</span>`;

  const ledArcVal = msg.enc_led_arc_deg;
  const camVal    = msg.enc_camera_deg;
  document.getElementById("enc-led-arc").innerHTML =
    ledArcVal == null ? `<span class="no-encoder">—</span>` : `${fmt(ledArcVal)}<span>°</span>`;
  document.getElementById("enc-camera").innerHTML =
    camVal == null ? `<span class="no-encoder">—</span>` : `${fmt(camVal)}<span>°</span>`;

  const errLedEl = document.getElementById("err-led");
  const errCamEl = document.getElementById("err-cam");

  if (ledArcVal == null) {
    errLedEl.textContent = "—";
    errLedEl.className = "error-value";
  } else if (typeof msg.led_error_deg === "number") {
    errLedEl.textContent = `${msg.led_error_deg >= 0 ? "+" : ""}${msg.led_error_deg.toFixed(4)}°`;
    errLedEl.className = `error-value ${errorClass(msg.led_error_deg)}`;
  }
  if (camVal == null) {
    errCamEl.textContent = "—";
    errCamEl.className = "error-value";
  } else if (typeof msg.camera_error_deg === "number") {
    errCamEl.textContent = `${msg.camera_error_deg >= 0 ? "+" : ""}${msg.camera_error_deg.toFixed(4)}°`;
    errCamEl.className = `error-value ${errorClass(msg.camera_error_deg)}`;
  }

  // Motor moving badges
  document.getElementById("m1-moving-badge").classList.toggle("hidden", !msg.motor1_moving);
  document.getElementById("m2-moving-badge").classList.toggle("hidden", !msg.motor2_moving);

  // Direction flip toggles
  const flip1 = document.getElementById("dir-flip-1");
  const flip2 = document.getElementById("dir-flip-2");
  if (flip1 && msg.dir_flip_1 !== undefined) flip1.checked = msg.dir_flip_1;
  if (flip2 && msg.dir_flip_2 !== undefined) flip2.checked = msg.dir_flip_2;

  if (msg.led_states) {
    msg.led_states.forEach((st, i) => {
      const btn = document.getElementById(`led-btn-${i}`);
      if (btn) {
        ledStates[i] = st;
        btn.classList.toggle("on", st === 1);
      }
    });
  }
});

on("move_done", (msg) => {
  logOk(`Motor ${msg.motor} reached ${msg.final_angle?.toFixed(4)}°`);
});

on("move_result", (msg) => {
  if (!msg.encoder_ok) {
    logWarn(`Motor ${msg.motor}: encoder unavailable — open-loop only (target ${msg.target?.toFixed(3)}°)`);
    return;
  }
  const tag = msg.converged ? logOk : logWarn;
  tag(`Motor ${msg.motor} landed at ${msg.achieved?.toFixed(4)}° `
      + `(residual ${msg.residual?.toFixed(4)}°, ${msg.iterations} iter`
      + `${msg.converged ? "" : ", not converged"})`);
});

on("error", (msg) => {
  logErr(`ESP32 error [${msg.code}]: ${msg.msg}`);
});

// ── Motor jog ─────────────────────────────────────────────────────────────────
document.querySelectorAll("[data-motor][data-dir]").forEach(btn => {
  btn.addEventListener("click", async () => {
    const motor = parseInt(btn.dataset.motor);
    const dir   = parseInt(btn.dataset.dir);
    const degEl = document.getElementById(`jog${motor}-deg`);
    const speedEl = document.getElementById(`jog${motor}-speed`);
    const degrees = parseFloat(degEl?.value) || 1.0;
    const speed = parseInt(speedEl?.value) || 30;
    await api.motorJog(motor, dir, degrees, speed);
    log(`Jog motor ${motor} dir=${dir} ${degrees}°`);
  });
});

// Speed sliders
[1, 2].forEach(m => {
  document.getElementById(`jog${m}-speed`)?.addEventListener("input", e => {
    document.getElementById(`jog${m}-speed-val`).textContent = e.target.value;
  });
  document.getElementById(`move${m}-speed`)?.addEventListener("input", e => {
    document.getElementById(`move${m}-speed-val`).textContent = e.target.value;
  });
});

// ── Motor absolute move ───────────────────────────────────────────────────────
document.getElementById("btn-move1").addEventListener("click", async () => {
  const angle = parseFloat(document.getElementById("move1-angle").value);
  const speed = parseInt(document.getElementById("move1-speed").value);
  const precise = document.getElementById("move1-precise").checked;
  await api.motorMove(1, angle, speed, precise);
  log(`Move motor 1 → ${angle}° @ ${speed}%${precise ? " (precise)" : ""}`);
});

document.getElementById("btn-move2").addEventListener("click", async () => {
  const angle = parseFloat(document.getElementById("move2-angle").value);
  const speed = parseInt(document.getElementById("move2-speed").value);
  const precise = document.getElementById("move2-precise").checked;
  await api.motorMove(2, angle, speed, precise);
  log(`Move motor 2 → ${angle}° @ ${speed}%${precise ? " (precise)" : ""}`);
});

// ── Motor stop ────────────────────────────────────────────────────────────────
document.getElementById("btn-stop1").addEventListener("click", async () => {
  await api.motorStop(1); logWarn("Motor 1 stopped");
});
document.getElementById("btn-stop2").addEventListener("click", async () => {
  await api.motorStop(2); logWarn("Motor 2 stopped");
});

// ── Encoder jitter (diagnostics) ────────────────────────────────────────────────
document.getElementById("btn-jitter")?.addEventListener("click", async (e) => {
  const btn = e.currentTarget;
  const secs = parseFloat(document.getElementById("jitter-seconds").value) || 10;
  btn.disabled = true;
  const label = btn.textContent;
  btn.textContent = `Measuring ${secs}s…`;
  logWarn(`Measuring encoder jitter for ${secs}s — keep the arm still…`);
  try {
    const r = await api.measureJitter(secs);
    const fmt = (s, name) => s
      ? logOk(`${name}: p-p ${s.peak_to_peak_deg.toFixed(4)}° `
              + `(σ ${s.stdev_deg.toFixed(4)}°, min ${s.min_deg.toFixed(4)}°, `
              + `max ${s.max_deg.toFixed(4)}°, n=${s.n})`)
      : logWarn(`${name}: no readings (encoder null — not wired/reporting?)`);
    fmt(r.camera_ome85, "Camera OME85");
    fmt(r.led_arc_as5600, "LED arc AS5600 #3");
  } catch (err) {
    logErr(`Jitter capture failed: ${err.message}`);
  } finally {
    btn.disabled = false;
    btn.textContent = label;
  }
});

// ── Home ──────────────────────────────────────────────────────────────────────
document.getElementById("btn-home-both").addEventListener("click", async () => {
  await api.motorHome(0); logOk("Home set for both motors");
});
document.getElementById("btn-home-led").addEventListener("click", async () => {
  await api.motorHome(2); logOk("Home set for LED arc motor");
});
document.getElementById("btn-home-cam").addEventListener("click", async () => {
  await api.motorHome(1); logOk("Home set for camera motor");
});

// ── Direction flip toggles ────────────────────────────────────────────────────
document.getElementById("dir-flip-1")?.addEventListener("change", async (e) => {
  await api.setConfig(e.target.checked, undefined);
  logOk(`Motor 1 direction ${e.target.checked ? "flipped" : "normal"}`);
});
document.getElementById("dir-flip-2")?.addEventListener("change", async (e) => {
  await api.setConfig(undefined, e.target.checked);
  logOk(`Motor 2 direction ${e.target.checked ? "flipped" : "normal"}`);
});

// ── LED controls ──────────────────────────────────────────────────────────────
const ledStates = new Array(7).fill(0);
const ledBrightness = new Array(7).fill(255);

const ledGrid = document.getElementById("led-grid");
for (let i = 0; i < 7; i++) {
  const cell = document.createElement("div");
  cell.className = "led-cell";

  const btn = document.createElement("button");
  btn.className = "led-btn";
  btn.id = `led-btn-${i}`;
  btn.textContent = `L${i + 1}`;
  btn.addEventListener("click", async () => {
    ledStates[i] = ledStates[i] ? 0 : 1;
    btn.classList.toggle("on", ledStates[i] === 1);
    await api.ledSet(i, ledStates[i], ledBrightness[i]);
    log(`LED ${i + 1} ${ledStates[i] ? "ON" : "OFF"} @ ${ledBrightness[i]}`);
  });

  const slider = document.createElement("input");
  slider.type = "range";
  slider.className = "led-brightness";
  slider.min = 0; slider.max = 255; slider.value = 255;
  slider.title = `LED ${i + 1} brightness`;
  slider.addEventListener("input", () => {
    ledBrightness[i] = parseInt(slider.value);
    if (ledStates[i]) api.ledSet(i, 1, ledBrightness[i]);
  });

  cell.appendChild(btn);
  cell.appendChild(slider);
  ledGrid.appendChild(cell);
}

document.getElementById("btn-led-all-on").addEventListener("click", async () => {
  ledStates.fill(1);
  document.querySelectorAll(".led-btn").forEach(b => b.classList.add("on"));
  await api.ledAll([...ledStates], [...ledBrightness]);
  log("All LEDs ON");
});

document.getElementById("btn-led-all-off").addEventListener("click", async () => {
  ledStates.fill(0);
  document.querySelectorAll(".led-btn").forEach(b => b.classList.remove("on"));
  await api.ledOff();
  log("All LEDs OFF");
});

// ── Camera ────────────────────────────────────────────────────────────────────
const camLed   = document.getElementById("cam-led").querySelector("circle");
const camLabel = document.getElementById("cam-label");

function setCameraOpen(info) {
  setLedColor(camLed, "#22c55e", "#22c55e");
  camLabel.textContent = `Camera: ${info?.model || "Open"}`;
  document.getElementById("btn-cam-open").classList.add("hidden");
  document.getElementById("btn-cam-close").classList.remove("hidden");
  // Camera is open but no MJPEG stream running yet — invite the user to it.
  // startLive() hides the placeholder once the stream begins.
  const ph = document.getElementById("camera-placeholder");
  if (!liveOn) ph.textContent = "Live Preview Available";
}

function setCameraClosed() {
  stopLive();
  setLedColor(camLed, "#ef4444", null);
  camLabel.textContent = "Camera off";
  document.getElementById("btn-cam-open").classList.remove("hidden");
  document.getElementById("btn-cam-close").classList.add("hidden");
  document.getElementById("camera-preview").classList.add("hidden");
  const ph = document.getElementById("camera-placeholder");
  ph.textContent = "Camera not open";
  ph.classList.remove("hidden");
}

on("camera_opened", (msg) => { setCameraOpen(msg.info); logOk(`Camera opened: ${msg.info?.model}`); });
on("camera_closed", () => { setCameraClosed(); logWarn("Camera closed"); });

document.getElementById("btn-cam-open").addEventListener("click", async () => {
  try {
    const res = await api.cameraOpen();
    setCameraOpen(res.info);
    logOk("Camera opened");
    // Apply the exposure and gain shown in the controls so the hardware starts
    // at a known value rather than whatever the SDK powered up with.
    const exp = parseFloat(document.getElementById("cam-exposure").value);
    const gain = parseFloat(document.getElementById("cam-gain").value);
    if (!Number.isNaN(exp) || !Number.isNaN(gain)) {
      await api.cameraSettings(
        Number.isNaN(exp) ? null : exp,
        Number.isNaN(gain) ? null : gain
      );
      logOk(`Camera settings applied: exposure=${exp}µs gain=${gain}`);
    }
  } catch (e) { logErr(`Camera open failed: ${e.message}`); }
});

document.getElementById("btn-cam-close").addEventListener("click", async () => {
  await api.cameraClose();
  setCameraClosed();
  logWarn("Camera closed");
});

document.getElementById("btn-cam-settings").addEventListener("click", async () => {
  const exp = parseFloat(document.getElementById("cam-exposure").value);
  const gain = parseFloat(document.getElementById("cam-gain").value);
  await api.cameraSettings(exp, gain);
  logOk(`Camera settings: exposure=${exp}µs gain=${gain}`);
});

document.getElementById("btn-cam-flip-x")?.addEventListener("click", async () => {
  const checked = document.getElementById("cam-flip-x").checked;
  await api.cameraSettings(null, null, checked, null);
  logOk(`Mirror X: ${checked}`);
});

document.getElementById("btn-cam-flip-y")?.addEventListener("click", async () => {
  const checked = document.getElementById("cam-flip-y").checked;
  await api.cameraSettings(null, null, null, checked);
  logOk(`Flip Y: ${checked}`);
});

// ── Live preview (MJPEG stream) ───────────────────────────────────────────────
let liveOn = false;

function stopLive() {
  if (!liveOn) return;
  liveOn = false;
  const img = document.getElementById("camera-preview");
  img.onerror = null;
  img.src = "";   // closing the <img> connection ends the backend MJPEG stream
  document.getElementById("btn-preview-live").textContent = "▶ Live";
}

function startLive() {
  const img = document.getElementById("camera-preview");
  const ph  = document.getElementById("camera-placeholder");
  liveOn = true;
  img.onerror = () => { if (liveOn) { logWarn("Live preview unavailable"); stopLive(); } };
  img.src = "/api/camera/stream?t=" + Date.now();
  img.classList.remove("hidden");
  ph.classList.add("hidden");
  document.getElementById("btn-preview-live").textContent = "⏸ Stop";
  logOk("Live preview started");
}

document.getElementById("btn-preview-live").addEventListener("click", () => {
  if (liveOn) stopLive(); else startLive();
});

document.getElementById("btn-preview-refresh").addEventListener("click", async () => {
  stopLive();  // single snapshot and live stream share the same <img>
  const img = document.getElementById("camera-preview");
  const ph  = document.getElementById("camera-placeholder");
  try {
    const resp = await fetch("/api/camera/preview");
    if (!resp.ok) throw new Error("Preview unavailable");
    const blob = await resp.blob();
    img.src = URL.createObjectURL(blob);
    img.classList.remove("hidden");
    ph.classList.add("hidden");
  } catch (e) { logWarn(`Preview: ${e.message}`); }
});

// ── Save Image (single manual capture, works idle or during live preview) ──────
document.getElementById("btn-cam-save").addEventListener("click", async () => {
  const folder = document.getElementById("output-folder").value.trim() || "./captures";
  const fmt = document.getElementById("img-format").value;
  // Same camera-arc slit angle the scan uses — a physical camera-mount property,
  // so it belongs in every saved image's sidecar, manual captures included.
  const arcAngle = parseFloat(document.getElementById("scan-camera-arc").value);
  const btn = document.getElementById("btn-cam-save");
  btn.disabled = true;
  try {
    const res = await api.cameraCapture(folder, fmt, Number.isNaN(arcAngle) ? null : arcAngle);  // gallery updates via image_captured
    logOk(`Image saved: ${res.path}`);
  } catch (e) {
    logErr(`Save image failed: ${e.message}`);
  } finally {
    btn.disabled = false;
  }
});

// ── Scan config helpers ───────────────────────────────────────────────────────

// Scan LED pattern checkboxes
// Fixed mounting angle of each LED on the arc (LED 1→0° … LED 7→60°, 10° apart).
// Mirrors LED_ARC_ANGLES_DEG in backend/models.py.
const LED_ARC_ANGLES_DEG = Array.from({ length: 7 }, (_, i) => i * 10);
const scanLedPattern = document.getElementById("scan-led-pattern");
for (let i = 0; i < 7; i++) {
  const label = document.createElement("label");
  label.className = "inline-label";
  label.style.flexDirection = "column";
  label.style.alignItems = "center";
  label.style.gap = "4px";
  label.style.fontSize = "11px";
  const cb = document.createElement("input");
  cb.type = "checkbox";
  cb.id = `scan-led-${i}`;
  cb.checked = (i === 0);   // default: only L1 selected
  cb.addEventListener("change", updateScanEstimate);
  label.appendChild(cb);
  label.appendChild(document.createTextNode(`L${i + 1}`));
  const ang = document.createElement("span");
  ang.textContent = `${LED_ARC_ANGLES_DEG[i]}°`;
  ang.style.color = "var(--text-dim)";
  ang.style.fontSize = "10px";
  label.appendChild(ang);
  scanLedPattern.appendChild(label);
}

// Scan speed sliders
document.getElementById("scan-led-speed").addEventListener("input", e => {
  document.getElementById("scan-led-speed-val").textContent = e.target.value;
});
document.getElementById("scan-cam-speed").addEventListener("input", e => {
  document.getElementById("scan-cam-speed-val").textContent = e.target.value;
});

function countPositions(start, stop, step) {
  if (step <= 0) return 0;
  return Math.floor((stop - start) / step) + 1;
}

// Mirror backend ScanAxis.positions (backend/models.py): inclusive of stop with
// a 1e-9 slack, rounded to 4 dp — so the client occlusion count matches the
// server's guard exactly.
function axisPositions(start, stop, step) {
  if (step <= 0) return [];
  const out = [];
  for (let a = start; a <= stop + 1e-9; a += step) out.push(Math.round(a * 1e4) / 1e4);
  return out;
}

// Wrap an angle difference into (−180, 180]; matches motion.normalize_deg.
function normalizeDeg(d) {
  return ((d + 180) % 360 + 360) % 360 - 180;
}

function updateScanEstimate() {
  const ledPos = axisPositions(
    parseFloat(document.getElementById("scan-led-start").value),
    parseFloat(document.getElementById("scan-led-stop").value),
    parseFloat(document.getElementById("scan-led-step").value),
  );
  const camPos = axisPositions(
    parseFloat(document.getElementById("scan-cam-start").value),
    parseFloat(document.getElementById("scan-cam-stop").value),
    parseFloat(document.getElementById("scan-cam-step").value),
  );
  const activeLeds = Array.from({ length: 7 }, (_, i) =>
    document.getElementById(`scan-led-${i}`)?.checked
  ).filter(Boolean).length;

  const keepout = parseFloat(document.getElementById("scan-keepout").value) || 0;
  // One-sided window (mirrors scan/controller.occluded_positions): the arc blocks
  // the lens only while it LEADS the camera by 0..keepout° — 0<normalize(led−cam)
  // <=keepout. Level/behind (lead<=0) or led past the window (lead>keepout) is
  // clear. keepout=0 disables the guard. Camera angles may be negative (sweep the
  // other way) — the math is on the signed configured angles directly.
  const blockedPairs = [];
  for (const l of ledPos) {
    for (const c of camPos) {
      const lead = normalizeDeg(l - c);
      if (lead > 0 && lead <= keepout) blockedPairs.push([l, c]);
    }
  }
  const blocked = blockedPairs.length;

  const usable = ledPos.length * camPos.length - blocked;
  const images = usable * activeLeds;
  document.getElementById("scan-pos-count").textContent =
    usable.toLocaleString() + (blocked ? ` (${blocked.toLocaleString()} blocked)` : "");
  document.getElementById("scan-img-count").textContent = images.toLocaleString();

  // Itemized list of skipped positions, shown whenever any are blocked (so it
  // stays visible through the scan run). Same math as the backend guard.
  const box = document.getElementById("scan-skipped-box");
  const listEl = document.getElementById("scan-skipped-list");
  document.getElementById("scan-skipped-count").textContent = blocked.toLocaleString();
  if (blocked > 0) {
    const fmtDeg = v => (Math.round(v * 1e4) / 1e4).toString();
    listEl.innerHTML = blockedPairs
      .map(([l, c]) => `LED ${fmtDeg(l)}° / CAM ${fmtDeg(c)}°`)
      .join("<br>");
    box.style.display = "";
  } else {
    listEl.innerHTML = "";
    box.style.display = "none";
  }
}

["scan-led-start","scan-led-stop","scan-led-step",
 "scan-cam-start","scan-cam-stop","scan-cam-step","scan-keepout"].forEach(id => {
  document.getElementById(id)?.addEventListener("input", updateScanEstimate);
});

updateScanEstimate();

// ── Scan start / pause / resume / abort ──────────────────────────────────────
let _scanRunning = false;
let _scanPaused  = false;

function setScanRunning(running, paused) {
  _scanRunning = running;
  _scanPaused  = paused;
  document.getElementById("btn-scan-start").classList.toggle("hidden", running);
  document.getElementById("btn-scan-pause").classList.toggle("hidden", !running || paused);
  document.getElementById("btn-scan-resume").classList.toggle("hidden", !paused);
  document.getElementById("btn-scan-abort").classList.toggle("hidden", !running);
  document.getElementById("scan-progress-card").style.display = running ? "block" : "none";
  document.getElementById("scan-pill").style.display = running ? "flex" : "none";
}

document.getElementById("btn-scan-start").addEventListener("click", async () => {
  const enabledLeds  = Array.from({ length: 7 }, (_, i) =>
    document.getElementById(`scan-led-${i}`)?.checked ?? true
  );
  const ledBrights   = Array.from({ length: 7 }, () => 255);

  const config = {
    led_axis: {
      start_deg:  parseFloat(document.getElementById("scan-led-start").value),
      stop_deg:   parseFloat(document.getElementById("scan-led-stop").value),
      step_deg:   parseFloat(document.getElementById("scan-led-step").value),
      speed_pct:  parseInt(document.getElementById("scan-led-speed").value),
    },
    camera_axis: {
      start_deg:  parseFloat(document.getElementById("scan-cam-start").value),
      stop_deg:   parseFloat(document.getElementById("scan-cam-stop").value),
      step_deg:   parseFloat(document.getElementById("scan-cam-step").value),
      speed_pct:  parseInt(document.getElementById("scan-cam-speed").value),
    },
    led_pattern: {
      enabled:    enabledLeds,
      brightness: ledBrights,
    },
    image_format:       document.getElementById("img-format").value,
    output_folder:      document.getElementById("output-folder").value.trim() || "./captures",
    camera_arc_angle_deg: parseFloat(document.getElementById("scan-camera-arc").value),
    move_simultaneously: document.getElementById("scan-simultaneous").checked,
    settle_s:           parseFloat(document.getElementById("scan-settle").value) || 5.0,
    precise_positioning: document.getElementById("scan-precise").checked,
    camera_keepout_deg: parseFloat(document.getElementById("scan-keepout").value) || 0,
  };

  try {
    const res = await api.scanStart(config);
    setScanRunning(true, false);
    logOk("Scan started");
    if (res && res.skipped) logWarn(res.skipped_message);
  } catch (e) {
    logErr(`Scan start failed: ${e.message}`);
  }
});

document.getElementById("btn-scan-pause").addEventListener("click", async () => {
  await api.scanPause();
  setScanRunning(true, true);
  logWarn("Scan paused");
});

document.getElementById("btn-scan-resume").addEventListener("click", async () => {
  await api.scanResume();
  setScanRunning(true, false);
  logOk("Scan resumed");
});

document.getElementById("btn-scan-abort").addEventListener("click", async () => {
  await api.scanAbort();
  setScanRunning(false, false);
  logWarn("Scan aborted");
});

// ── Scan progress updates ─────────────────────────────────────────────────────
on("scan_progress", (msg) => {
  if (!msg.running && _scanRunning) {
    setScanRunning(false, false);
    logOk(`Scan complete — ${msg.images_captured} images captured`);
  }

  const pct = msg.total_positions > 0
    ? (msg.current_position / msg.total_positions * 100).toFixed(1)
    : 0;
  document.getElementById("scan-progress-fill").style.width = `${pct}%`;
  document.getElementById("scan-progress-text").textContent =
    `${msg.current_position} / ${msg.total_positions} (${pct}%)`;
  document.getElementById("scan-images-text").textContent =
    `${msg.images_captured} images`;
  document.getElementById("scan-cur-led").textContent =
    typeof msg.current_led_pos_deg === "number" ? `${msg.current_led_pos_deg.toFixed(2)}°` : "—";
  document.getElementById("scan-cur-cam").textContent =
    typeof msg.current_cam_pos_deg === "number" ? `${msg.current_cam_pos_deg.toFixed(2)}°` : "—";
  document.getElementById("scan-pill-label").textContent =
    `${msg.current_position} / ${msg.total_positions}`;

  if (msg.errors && msg.errors.length > 0) {
    const errEl = document.getElementById("scan-errors");
    errEl.classList.remove("hidden");
    errEl.textContent = msg.errors.slice(-3).join("\n");
  }
});

// ── Image captured ────────────────────────────────────────────────────────────
const gallery = document.getElementById("gallery-grid");
const galleryEmpty = document.getElementById("gallery-empty");

on("image_captured", (msg) => {
  galleryEmpty.classList.add("hidden");

  const item = document.createElement("div");
  item.className = "gallery-item";

  const img = document.createElement("img");
  img.src = `data:image/jpeg;base64,${msg.preview_b64}`;
  img.alt = msg.path;

  const caption = document.createElement("div");
  caption.className = "gallery-caption";
  caption.textContent = msg.path.split(/[\\/]/).pop();
  caption.title = msg.path;

  item.appendChild(img);
  item.appendChild(caption);
  gallery.prepend(item);

  // Lightbox
  item.addEventListener("click", () => openLightbox(img.src, msg.path));
});

document.getElementById("btn-clear-gallery").addEventListener("click", () => {
  gallery.innerHTML = "";
  galleryEmpty.classList.remove("hidden");
});

// ── Lightbox ──────────────────────────────────────────────────────────────────
const lightbox = document.getElementById("lightbox");
const lightboxImg = document.getElementById("lightbox-img");
const lightboxCaption = document.getElementById("lightbox-caption");

function openLightbox(src, caption) {
  lightboxImg.src = src;
  lightboxCaption.textContent = caption;
  lightbox.style.display = "flex";
}

document.getElementById("btn-lightbox-close").addEventListener("click", () => {
  lightbox.style.display = "none";
});

lightbox.addEventListener("click", (e) => {
  if (e.target === lightbox) lightbox.style.display = "none";
});
