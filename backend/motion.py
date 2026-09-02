"""
Closed-loop "perfect angle" positioning.

The steppers are open-loop: commanding MOVE to 10° lands *near* 10°, but the
absolute output encoder (OME85 for the camera, AS5600 #3 for the LED arc) is the
ground truth and reads e.g. 9.589° or 10.237°. These helpers run a proportional
correction loop that nudges the *absolute MOVE setpoint* until the *encoder*
reads the target within a tolerance — with a deadband, gain < 1, iteration cap,
and stall guards so it converges instead of hunting forever.

Corrections use absolute MOVE, NOT relative JOG. On this rig a small JOG loses
most of its travel to gear backlash — measured: a 0.5° jog moved ~nothing, a 5°
jog produced only 4.24° of output — whereas an absolute MOVE to 5° lands at
5.02°. So the loop re-issues MOVE to an adjusted setpoint each iteration rather
than jogging by the residual.

Pure functions over an `ESP32Connection`; no module globals. The connection's
`arm_move_done(motor)` / MOVE_DONE-future machinery is what lets us await the
settled angle after each nudge. `final_angle` may be None (encoder read failed) —
callers fall back to open-loop and flag it.

Angles are output-shaft degrees; JOG `degrees` and MOVE_DONE `final_angle` are in
the same units, so an encoder error maps directly onto a jog magnitude.
"""
import asyncio
import logging
import statistics
from typing import Optional

from .esp32.protocol import cmd_move, cmd_jog

log = logging.getLogger(__name__)


def normalize_deg(diff: float) -> float:
    """Wrap an angle difference into (-180, 180]."""
    return (diff + 180) % 360 - 180


def jitter_stats(samples: list) -> Optional[dict]:
    """Peak-to-peak scatter of a resting-encoder capture, wrap-safe.

    Each sample is an absolute output-shaft angle in degrees (or None when the
    encoder read failed that cycle — those are dropped). Spread is measured
    relative to the first valid sample and wrapped into (-180, 180] so a capture
    straddling the 360/0 seam doesn't report a bogus ~360° range.

    Returns {n, mean_deg, min_deg, max_deg, peak_to_peak_deg, stdev_deg}, or None
    if fewer than 2 valid samples were collected.
    """
    vals = [s for s in samples if s is not None]
    if len(vals) < 2:
        return None
    ref = vals[0]
    deltas = [normalize_deg(v - ref) for v in vals]
    lo, hi = min(deltas), max(deltas)
    return {
        "n": len(vals),
        "mean_deg": round(ref + statistics.fmean(deltas), 4),
        "min_deg": round(ref + lo, 4),
        "max_deg": round(ref + hi, 4),
        "peak_to_peak_deg": round(hi - lo, 4),
        "stdev_deg": round(statistics.pstdev(deltas), 4),
    }


async def move_and_wait(esp, motor: int, angle: float, speed: int,
                        timeout: float = 30.0) -> Optional[float]:
    """MOVE `motor` to `angle` and return the settled output-encoder angle
    (None on encoder-read failure or timeout)."""
    fut = esp.arm_move_done(motor)
    await esp.send_raw(cmd_move(motor, angle, speed))
    try:
        return await asyncio.wait_for(fut, timeout=timeout)
    except asyncio.TimeoutError:
        log.warning("MOVE motor %d → %.4f° timed out after %.0fs", motor, angle, timeout)
        return None


async def jog_and_wait(esp, motor: int, direction: int, degrees: float,
                       speed: int, timeout: float = 15.0) -> Optional[float]:
    """JOG `motor` by a relative `degrees` in `direction` (+1/-1) and return the
    settled output-encoder angle (None on encoder-read failure or timeout)."""
    fut = esp.arm_move_done(motor)
    await esp.send_raw(cmd_jog(motor, direction, degrees, speed))
    try:
        return await asyncio.wait_for(fut, timeout=timeout)
    except asyncio.TimeoutError:
        log.warning("JOG motor %d %+d %.4f° timed out after %.0fs",
                    motor, direction, degrees, timeout)
        return None


async def refine(esp, motor: int, target: float, actual: Optional[float], *,
                 tol: float, gain: float, max_iter: int, jog_speed: int,
                 min_step: float) -> dict:
    """Proportional correction loop. Seeded with `actual` (the settled angle from
    the coarse move). Nudges toward `target` until within `tol`, the correction
    falls below `min_step` (one motor µstep), or `max_iter` is hit — and bails if
    the error stops shrinking (backlash / wrong-direction guard).

    Returns {achieved, residual, iterations, converged, encoder_ok}.
    """
    if actual is None:
        # Coarse move gave no encoder reading — stay open-loop, flag it.
        return {"achieved": None, "residual": None, "iterations": 0,
                "converged": False, "encoder_ok": False}

    err = normalize_deg(target - actual)
    iterations = 0
    converged = abs(err) <= tol

    for i in range(max_iter):
        if abs(err) <= tol:
            converged = True
            break
        if abs(err) < min_step:
            # Needed nudge is finer than the motor can resolve — as good as it gets.
            converged = True
            break

        prev_err = err
        direction = 1 if err > 0 else -1
        nudge = abs(err) * gain
        new_actual = await jog_and_wait(esp, motor, direction, nudge, jog_speed)
        iterations = i + 1

        if new_actual is None:
            # Lost the encoder mid-loop; keep the last good residual, flag it.
            return {"achieved": actual, "residual": round(err, 4),
                    "iterations": iterations, "converged": False,
                    "encoder_ok": False}

        actual = new_actual
        err = normalize_deg(target - actual)

        # Stall / wrong-direction guard: if the error didn't shrink, stop rather
        # than hunt. (Backlash slack or an inverted axis would otherwise loop.)
        if abs(err) >= abs(prev_err):
            break

    converged = converged or abs(err) <= tol
    return {"achieved": round(actual, 4), "residual": round(err, 4),
            "iterations": iterations, "converged": converged,
            "encoder_ok": True}


async def move_precise(esp, motor: int, target: float, speed: int, *,
                       tol: float, gain: float, max_iter: int, jog_speed: int,
                       min_step: float) -> dict:
    """Coarse MOVE to `target`, then closed-loop refine onto the encoder.

    Returns the `refine` result dict; on a coarse-move encoder failure the loop
    is skipped and {encoder_ok: False} is returned (open-loop position stands)."""
    actual = await move_and_wait(esp, motor, target, speed)
    return await refine(esp, motor, target, actual, tol=tol, gain=gain,
                        max_iter=max_iter, jog_speed=jog_speed, min_step=min_step)
