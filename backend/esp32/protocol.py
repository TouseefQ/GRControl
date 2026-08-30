import json
from typing import Any, Optional


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


def cmd_jog(motor: int, direction: int, degrees: float, speed: int = 30) -> bytes:
    return encode({"type": "JOG", "motor": motor, "direction": direction,
                   "degrees": round(degrees, 4), "speed": speed})


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


def cmd_set_config(
    dir_flip_1: Optional[bool] = None,
    dir_flip_2: Optional[bool] = None,
    max_speed_sps: Optional[float] = None,
) -> bytes:
    msg: dict = {"type": "SET_CONFIG"}
    if dir_flip_1 is not None:
        msg["dir_flip_1"] = dir_flip_1
    if dir_flip_2 is not None:
        msg["dir_flip_2"] = dir_flip_2
    if max_speed_sps is not None:
        msg["max_speed_sps"] = max_speed_sps
    return encode(msg)
