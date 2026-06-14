/**
 * log.js — in-browser event log
 */

const MAX_ENTRIES = 200;

const list = document.getElementById("log-list");
document.getElementById("btn-clear-log").addEventListener("click", () => {
  list.innerHTML = "";
});

export function log(msg, level = "info") {
  const now = new Date();
  const time = `${String(now.getHours()).padStart(2,"0")}:${String(now.getMinutes()).padStart(2,"0")}:${String(now.getSeconds()).padStart(2,"0")}`;
  const el = document.createElement("div");
  el.className = `log-entry ${level}`;
  el.textContent = `[${time}] ${msg}`;
  list.prepend(el);
  while (list.children.length > MAX_ENTRIES) {
    list.removeChild(list.lastChild);
  }
}

export function logOk(msg)   { log(msg, "ok"); }
export function logWarn(msg) { log(msg, "warn"); }
export function logErr(msg)  { log(msg, "error"); }
