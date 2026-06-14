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
  document.getElementById("enc-led-arc").innerHTML = `${fmt(msg.enc_led_arc_deg)}<span>°</span>`;
  document.getElementById("enc-camera").innerHTML = `${fmt(msg.enc_camera_deg)}<span>°</span>`;

  const errLedEl = document.getElementById("err-led");
  const errCamEl = document.getElementById("err-cam");

  if (typeof msg.led_error_deg === "number") {
    errLedEl.textContent = `${msg.led_error_deg >= 0 ? "+" : ""}${msg.led_error_deg.toFixed(4)}°`;
    errLedEl.className = `error-value ${errorClass(msg.led_error_deg)}`;
  }
  if (typeof msg.camera_error_deg === "number") {
    errCamEl.textContent = `${msg.camera_error_deg >= 0 ? "+" : ""}${msg.camera_error_deg.toFixed(4)}°`;
    errCamEl.className = `error-value ${errorClass(msg.camera_error_deg)}`;
  }

  // Motor moving badges
  document.getElementById("m1-moving-badge").classList.toggle("hidden", !msg.motor1_moving);
  document.getElementById("m2-moving-badge").classList.toggle("hidden", !msg.motor2_moving);

  // Sync LED button states
  if (msg.led_states) {
    msg.led_states.forEach((st, i) => {
      document.getElementById(`led-btn-${i}`)?.classList.toggle("on", st === 1);
    });
  }
});

on("move_done", (msg) => {
  logOk(`Motor ${msg.motor} reached ${msg.final_angle?.toFixed(4)}°`);
});

on("error", (msg) => {
  logErr(`ESP32 error [${msg.code}]: ${msg.msg}`);
});

// ── Motor jog ─────────────────────────────────────────────────────────────────
document.querySelectorAll("[data-motor][data-dir]").forEach(btn => {
  btn.addEventListener("click", async () => {
    const motor = parseInt(btn.dataset.motor);
    const dir   = parseInt(btn.dataset.dir);
    const stepsEl = document.getElementById(`jog${motor}-steps`);
    const speedEl = document.getElementById(`jog${motor}-speed`);
    const steps = parseInt(stepsEl?.value) || 100;
    const speed = parseInt(speedEl?.value) || 30;
    await api.motorJog(motor, dir, steps, speed);
    log(`Jog motor ${motor} dir=${dir} steps=${steps}`);
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
  await api.motorMove(1, angle, speed);
  log(`Move motor 1 → ${angle}° @ ${speed}%`);
});

document.getElementById("btn-move2").addEventListener("click", async () => {
  const angle = parseFloat(document.getElementById("move2-angle").value);
  const speed = parseInt(document.getElementById("move2-speed").value);
  await api.motorMove(2, angle, speed);
  log(`Move motor 2 → ${angle}° @ ${speed}%`);
});

// ── Motor stop ────────────────────────────────────────────────────────────────
document.getElementById("btn-stop1").addEventListener("click", async () => {
  await api.motorStop(1); logWarn("Motor 1 stopped");
});
document.getElementById("btn-stop2").addEventListener("click", async () => {
  await api.motorStop(2); logWarn("Motor 2 stopped");
});

// ── Home ──────────────────────────────────────────────────────────────────────
document.getElementById("btn-home-both").addEventListener("click", async () => {
  await api.motorHome(0); logOk("Home set for both motors");
});
document.getElementById("btn-home-led").addEventListener("click", async () => {
  await api.motorHome(1); logOk("Home set for LED arc motor");
});
document.getElementById("btn-home-cam").addEventListener("click", async () => {
  await api.motorHome(2); logOk("Home set for camera motor");
});

// ── LED controls ──────────────────────────────────────────────────────────────
const ledStates = new Array(7).fill(0);
const ledBrightness = new Array(7).fill(200);

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
  slider.min = 0; slider.max = 255; slider.value = 200;
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
}

function setCameraClosed() {
  setLedColor(camLed, "#ef4444", null);
  camLabel.textContent = "Camera off";
  document.getElementById("btn-cam-open").classList.remove("hidden");
  document.getElementById("btn-cam-close").classList.add("hidden");
  document.getElementById("camera-preview").classList.add("hidden");
  document.getElementById("camera-placeholder").classList.remove("hidden");
}

on("camera_opened", (msg) => { setCameraOpen(msg.info); logOk(`Camera opened: ${msg.info?.model}`); });
on("camera_closed", () => { setCameraClosed(); logWarn("Camera closed"); });

document.getElementById("btn-cam-open").addEventListener("click", async () => {
  try {
    const res = await api.cameraOpen();
    setCameraOpen(res.info);
    logOk("Camera opened");
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

document.getElementById("btn-preview-refresh").addEventListener("click", async () => {
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

// ── Scan config helpers ───────────────────────────────────────────────────────

// Scan LED pattern checkboxes
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
  cb.checked = true;
  cb.addEventListener("change", updateScanEstimate);
  label.appendChild(cb);
  label.appendChild(document.createTextNode(`L${i + 1}`));
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

function updateScanEstimate() {
  const ledCount = countPositions(
    parseFloat(document.getElementById("scan-led-start").value),
    parseFloat(document.getElementById("scan-led-stop").value),
    parseFloat(document.getElementById("scan-led-step").value),
  );
  const camCount = countPositions(
    parseFloat(document.getElementById("scan-cam-start").value),
    parseFloat(document.getElementById("scan-cam-stop").value),
    parseFloat(document.getElementById("scan-cam-step").value),
  );
  const activeLeds = Array.from({ length: 7 }, (_, i) =>
    document.getElementById(`scan-led-${i}`)?.checked
  ).filter(Boolean).length;

  const positions = ledCount * camCount;
  const images    = positions * activeLeds;
  document.getElementById("scan-pos-count").textContent = positions.toLocaleString();
  document.getElementById("scan-img-count").textContent = images.toLocaleString();
}

["scan-led-start","scan-led-stop","scan-led-step",
 "scan-cam-start","scan-cam-stop","scan-cam-step"].forEach(id => {
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
  const ledBrights   = Array.from({ length: 7 }, () => 200); // fixed at 200 for scan

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
    move_simultaneously: document.getElementById("scan-simultaneous").checked,
  };

  try {
    await api.scanStart(config);
    setScanRunning(true, false);
    logOk("Scan started");
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
