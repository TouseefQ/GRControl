import json
from typing import Any


def encode(msg: dict) -> bytes:
    return (json.dumps(msg) + "\n").encode("utf-8")


def decode(line: str) -> dict:
    return json.loads(line.strip())


# ── Command builders ──────────────────────────────────────────────────────────

def cmd_ping() -> bytes:
    return encode({"type": "PING"})


def cmd_get_state() -> bytes:
    return encode({"type": "GET_STATE"})


def cmd_move(motor: int, angle: float, speed: int = 80) -> bytes:
    return encode({"type": "MOVE", "motor": motor, "angle": round(angle, 4), "speed": speed})


def cmd_jog(motor: int, direction: int, steps: int, speed: int = 30) -> bytes:
    return encode({"type": "JOG", "motor": motor, "direction": direction,
                   "steps": steps, "speed": speed})


def cmd_stop(motor: int = 0) -> bytes:
    return encode({"type": "STOP", "motor": motor})


def cmd_set_home(motor: int = 0) -> bytes:
    return encode({"type": "SET_HOME", "motor": motor})


def cmd_led_set(index: int, state: int, brightness: int) -> bytes:
    return encode({"type": "LED_SET", "index": index, "state": state,
                   "brightness": brightness})


def cmd_led_all(states: list[int], brightness: list[int]) -> bytes:
    return encode({"type": "LED_ALL", "states": states, "brightness": brightness})


def cmd_led_off_all() -> bytes:
    return encode({"type": "LED_OFF_ALL"})


def cmd_set_telemetry(interval_ms: int) -> bytes:
    return encode({"type": "SET_TELEMETRY", "interval_ms": interval_ms})
