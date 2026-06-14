/**
 * ws.js — WebSocket client with auto-reconnect
 * Dispatches incoming events to registered handlers.
 */

const RECONNECT_DELAY_MS = 3000;
const handlers = {};

let ws = null;
let _onStatusChange = null;

export function onStatusChange(fn) { _onStatusChange = fn; }

export function on(event, fn) {
  if (!handlers[event]) handlers[event] = [];
  handlers[event].push(fn);
}

function dispatch(event, data) {
  (handlers[event] || []).forEach(fn => fn(data));
  (handlers["*"] || []).forEach(fn => fn(event, data));
}

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);

  ws.addEventListener("open", () => {
    _onStatusChange?.("ws_open");
    dispatch("ws_open", {});
  });

  ws.addEventListener("close", () => {
    _onStatusChange?.("ws_closed");
    dispatch("ws_closed", {});
    setTimeout(connect, RECONNECT_DELAY_MS);
  });

  ws.addEventListener("error", () => {
    ws.close();
  });

  ws.addEventListener("message", (e) => {
    try {
      const msg = JSON.parse(e.data);
      dispatch(msg.event || "__raw", msg);
    } catch { /* ignore malformed */ }
  });
}

connect();
