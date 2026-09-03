/**
 * api.js — thin wrappers around the FastAPI REST endpoints
 */

const BASE = "";  // same origin

export async function getPorts() {
  const r = await fetch(`${BASE}/api/ports`);
  return r.json();
}

export async function connectSerial(port) {
  const r = await fetch(`${BASE}/api/connect`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ mode: "serial", port }),
  });
  if (!r.ok) throw new Error((await r.json()).detail);
  return r.json();
}

export async function connectTCP(host) {
  const r = await fetch(`${BASE}/api/connect`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ mode: "tcp", host }),
  });
  if (!r.ok) throw new Error((await r.json()).detail);
  return r.json();
}

export async function disconnect() {
  const r = await fetch(`${BASE}/api/disconnect`, { method: "POST" });
  return r.json();
}

export async function motorMove(motor, angle, speed, precise) {
  await fetch(`${BASE}/api/motor/move`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ motor, angle, speed, precise }),
  });
}

export async function motorJog(motor, direction, degrees, speed) {
  await fetch(`${BASE}/api/motor/jog`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ motor, direction, degrees, speed }),
  });
}

export async function setConfig(dir_flip_1, dir_flip_2, max_speed_sps) {
  const body = {};
  if (dir_flip_1 !== undefined) body.dir_flip_1 = dir_flip_1;
  if (dir_flip_2 !== undefined) body.dir_flip_2 = dir_flip_2;
  if (max_speed_sps !== undefined) body.max_speed_sps = max_speed_sps;
  await fetch(`${BASE}/api/motor/config`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export async function motorStop(motor) {
  await fetch(`${BASE}/api/motor/stop`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ motor }),
  });
}

export async function motorHome(motor) {
  await fetch(`${BASE}/api/motor/home`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ motor }),
  });
}

export async function measureJitter(duration_s = 10) {
  const r = await fetch(`${BASE}/api/encoder/jitter`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ duration_s }),
  });
  if (!r.ok) throw new Error((await r.json()).detail);
  return r.json();
}

export async function ledSet(index, state, brightness) {
  await fetch(`${BASE}/api/led/set`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ index, state, brightness }),
  });
}

export async function ledAll(states, brightness) {
  await fetch(`${BASE}/api/led/all`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ states, brightness }),
  });
}

export async function ledOff() {
  await fetch(`${BASE}/api/led/off`, { method: "POST" });
}

export async function cameraOpen() {
  const r = await fetch(`${BASE}/api/camera/open`, { method: "POST" });
  if (!r.ok) throw new Error((await r.json()).detail);
  return r.json();
}

export async function cameraClose() {
  await fetch(`${BASE}/api/camera/close`, { method: "POST" });
}

export async function cameraSettings(exposure_us, gain, reverse_x = null, reverse_y = null) {
  const body = { exposure_us, gain };
  if (reverse_x !== null) body.reverse_x = reverse_x;
  if (reverse_y !== null) body.reverse_y = reverse_y;
  const r = await fetch(`${BASE}/api/camera/settings`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return r.json();
}

export async function cameraCapture(output_folder, image_format) {
  const r = await fetch(`${BASE}/api/camera/capture`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ output_folder, image_format }),
  });
  if (!r.ok) throw new Error((await r.json()).detail);
  return r.json();
}

export async function scanStart(config) {
  const r = await fetch(`${BASE}/api/scan/start`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(config),
  });
  if (!r.ok) throw new Error((await r.json()).detail);
  return r.json();
}

export async function scanPause() {
  await fetch(`${BASE}/api/scan/pause`, { method: "POST" });
}

export async function scanResume() {
  await fetch(`${BASE}/api/scan/resume`, { method: "POST" });
}

export async function scanAbort() {
  await fetch(`${BASE}/api/scan/abort`, { method: "POST" });
}
