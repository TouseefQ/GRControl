"""
Scan sequence state machine.

For each combination of (led_position, camera_position):
  - Move both motors (optionally simultaneously)
  - Wait for both MOVE_DONE events
  - For each enabled LED:
      1. Turn LED on
      2. Capture image with metadata
      3. Turn LED off
  - Emit progress updates throughout

The controller is decoupled from the WebSocket layer — it calls async
callbacks that the main app wires up.
"""
import asyncio
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Callable, Awaitable, Optional

from ..models import ScanConfig, ScanProgress, EncoderState, LED_ARC_ANGLES_DEG
from ..esp32.protocol import (
    cmd_move, cmd_stop, cmd_led_set, cmd_led_off_all
)
from .. import motion

log = logging.getLogger(__name__)

ProgressCallback = Callable[[ScanProgress], Awaitable[None]]
ImageCallback = Callable[[str, bytes], Awaitable[None]]  # (path, jpeg_preview)


class ScanController:
    def __init__(self, esp32_conn, camera, settings):
        self._esp = esp32_conn
        self._camera = camera
        self._settings = settings

        self._progress = ScanProgress()
        self._config: Optional[ScanConfig] = None
        self._progress_cb: Optional[ProgressCallback] = None
        self._image_cb: Optional[ImageCallback] = None

        # Events signalled when MOVE_DONE arrives for each motor
        self._move_done_motor1 = asyncio.Event()
        self._move_done_motor2 = asyncio.Event()

        # Last settled output-encoder angle per motor (from MOVE_DONE), used to
        # seed the closed-loop refinement; and the residual left after it, for
        # the capture metadata.
        self._last_final: dict[int, Optional[float]] = {1: None, 2: None}
        self._last_residual: dict[int, Optional[float]] = {1: None, 2: None}

        # Current encoder readings, kept fresh from telemetry
        self.current_encoder = EncoderState()

        self._task: Optional[asyncio.Task] = None

    def set_progress_callback(self, cb: ProgressCallback):
        self._progress_cb = cb

    def set_image_callback(self, cb: ImageCallback):
        self._image_cb = cb

    # ── Called by the ESP32 connection dispatcher ─────────────────────────────

    async def on_esp32_message(self, msg: dict):
        mtype = msg.get("type")
        if mtype == "STATE":
            self.current_encoder.motor1_deg = msg.get("enc_motor1_deg", 0.0)
            self.current_encoder.motor2_deg = msg.get("enc_motor2_deg", 0.0)
            self.current_encoder.led_arc_deg = msg.get("enc_led_arc_deg", 0.0)
            self.current_encoder.camera_deg = msg.get("enc_camera_deg", 0.0)
        elif mtype == "MOVE_DONE":
            motor = msg.get("motor")
            if motor == 1:
                self._last_final[1] = msg.get("final_angle")
                self._move_done_motor1.set()
            elif motor == 2:
                self._last_final[2] = msg.get("final_angle")
                self._move_done_motor2.set()

    # ── Public control ────────────────────────────────────────────────────────

    def keepout_deg(self, config: ScanConfig) -> float:
        """Effective LED-arc lead margin (deg) for this run: the per-scan override
        if set, else the workspace default. See occluded_positions for meaning."""
        ko = config.camera_keepout_deg
        return self._settings.camera_keepout_deg if ko is None else ko

    @staticmethod
    def _cam_cmd_deg(cam_pos: float, reverse: bool) -> float:
        """Angle to COMMAND the camera motor for a configured position. Reverse
        drives it anti-clockwise via a negative absolute angle (the firmware's
        MOVE is a linear absolute target, so −p sweeps CCW)."""
        return round(-cam_pos, 4) if reverse else cam_pos

    @staticmethod
    def _cam_view_deg(cam_pos: float, reverse: bool) -> float:
        """Camera viewing angle to RECORD/display for a configured position.
        Reverse maps 0..180 → 360..180 (p → 360−p); forward is unchanged."""
        return round(360.0 - cam_pos, 4) if reverse else cam_pos

    def occluded_positions(self, config: ScanConfig) -> list[tuple[float, float]]:
        """Every (led_pos, cam_pos) grid pair where the LED arc leads the camera
        and would sit in front of the lens, so it must be skipped.

        At home (both 0°) the LED arc rests at the edge of the lens. As the arc
        rotates AHEAD of the camera it moves into the lens's view; level with or
        behind the camera it's clear. So a pair is occluded when the arc leads the
        camera by more than the keep-out margin:

            normalize(led − cam_view) > keepout

        keepout (deg, default 0) is how far the arc may lead before it blocks:
          0  → capture only led ≤ cam (arc level or behind); skip led > cam
          +m → tolerate the arc leading by up to m° before skipping (permissive)
          −m → also skip within m° below the camera (more conservative)

        This is a DIRECTIONAL rule, not a symmetric window: a pair with the arc
        behind the camera (led < cam) is never skipped. Uses the recorded camera
        angle (cam_view) so reverse sweeps stay correct; the returned cam_pos is
        the raw configured value for membership tests."""
        keepout = self.keepout_deg(config)
        bad = []
        for led_pos in config.led_axis.positions:
            for cam_pos in config.camera_axis.positions:
                cam_view = self._cam_view_deg(cam_pos, config.camera_reverse)
                if motion.normalize_deg(led_pos - cam_view) > keepout:
                    bad.append((led_pos, cam_pos))
        return bad

    async def start(self, config: ScanConfig) -> bool:
        # Only refuse if a scan task is genuinely still alive. The `running`
        # flag alone can get stuck True (hung move, left paused, crash before
        # the finally block), which would otherwise lock out all future scans.
        if self._task and not self._task.done():
            log.warning("Scan already running")
            return False
        # Clear any stale state left over from a previous run.
        self._progress.running = False
        self._progress.paused = False
        self._config = config
        self._task = asyncio.create_task(self._run_scan())
        return True

    async def pause(self):
        if self._progress.running:
            self._progress.paused = True
            await self._esp.send_raw(cmd_stop(0))
            await self._emit_progress()

    async def resume(self):
        self._progress.paused = False
        await self._emit_progress()

    async def abort(self):
        if self._task:
            self._task.cancel()
        await self._esp.send_raw(cmd_stop(0))
        await self._esp.send_raw(cmd_led_off_all())
        self._progress.running = False
        self._progress.paused = False
        await self._emit_progress()

    # ── Scan state machine ────────────────────────────────────────────────────

    async def _run_scan(self):
        cfg = self._config
        led_positions = cfg.led_axis.positions
        cam_positions = cfg.camera_axis.positions
        active_leds = [i for i, en in enumerate(cfg.led_pattern.enabled) if en]

        # Positions where the LED arc would block the lens are skipped, not
        # scanned. Size the total to only the pairs we will actually capture.
        occluded = set(self.occluded_positions(cfg))
        scanned_pairs = len(led_positions) * len(cam_positions) - len(occluded)
        total = scanned_pairs * len(active_leds)
        self._progress = ScanProgress(
            running=True,
            total_positions=total,
            current_position=0,
            images_captured=0,
        )
        await self._emit_progress()
        if occluded:
            log.info("Skipping %d occluded position(s) within the %g° camera keep-out",
                     len(occluded), self.keepout_deg(cfg))

        # Ensure output folder exists
        Path(cfg.output_folder).mkdir(parents=True, exist_ok=True)

        try:
            # Camera is the OUTER axis: it holds a position while the LED arc
            # sweeps 0..(camera angle), then the camera advances and the arc
            # resets to 0. This matches the physical scan and the occlusion rule
            # (the arc is skipped wherever it would lead the camera).
            for cam_pos in cam_positions:
                for led_pos in led_positions:
                    if (led_pos, cam_pos) in occluded:
                        continue  # LED arc leads the camera here — skip

                    # Reverse (anti-clockwise) camera arc: command the camera
                    # motor to the negative angle so it sweeps CCW, but record and
                    # display the equivalent 360−p viewing angle. Forward mode:
                    # both are just cam_pos.
                    cam_cmd = self._cam_cmd_deg(cam_pos, cfg.camera_reverse)
                    cam_view = self._cam_view_deg(cam_pos, cfg.camera_reverse)

                    # Pause check
                    while self._progress.paused:
                        await asyncio.sleep(0.1)

                    # Move motors (camera to the commanded angle, LED to led_pos)
                    await self._move_to(led_pos, cam_cmd, cfg)

                    # Per-LED capture
                    for led_idx in active_leds:
                        while self._progress.paused:
                            await asyncio.sleep(0.1)

                        brightness = cfg.led_pattern.brightness[led_idx]

                        # Turn LED on and hold it lit while the arm settles,
                        # so the camera has time to expose. This dwell (default
                        # 5 s) replaces the old 50 ms blink that was too fast.
                        await self._esp.send_raw(
                            cmd_led_set(led_idx, 1, brightness)
                        )
                        settle = getattr(cfg, "settle_s", 5.0)
                        # Stay responsive to pause/abort during a long dwell.
                        waited = 0.0
                        while waited < settle:
                            if self._progress.paused:
                                break
                            step = min(0.1, settle - waited)
                            await asyncio.sleep(step)
                            waited += step

                        # Capture
                        filename = self._make_filename(
                            cfg.output_folder, led_pos, cam_view, led_idx, cfg.image_format
                        )
                        metadata = self._make_metadata(led_pos, cam_view, led_idx, brightness)
                        saved_path = await self._camera.capture(
                            filename, cfg.image_format, metadata
                        )

                        # Turn LED off
                        await self._esp.send_raw(cmd_led_set(led_idx, 0, 0))

                        if saved_path:
                            self._progress.images_captured += 1
                            if self._image_cb:
                                preview = await self._camera.grab_preview_jpeg()
                                if preview:
                                    await self._image_cb(saved_path, preview)
                        else:
                            self._progress.errors.append(
                                f"Capture failed at LED={led_pos}° CAM={cam_view}° LED#{led_idx}"
                            )

                        self._progress.current_position += 1
                        self._progress.current_led_pos_deg = led_pos
                        self._progress.current_cam_pos_deg = cam_view
                        self._progress.current_led_index = led_idx
                        await self._emit_progress()

        except asyncio.CancelledError:
            log.info("Scan aborted")
        except Exception as e:
            log.error("Scan error: %s", e)
            self._progress.errors.append(str(e))
        finally:
            await self._esp.send_raw(cmd_led_off_all())
            self._progress.running = False
            self._progress.paused = False
            await self._emit_progress()

    async def _move_to(self, led_pos: float, cam_pos: float, cfg: ScanConfig):
        self._move_done_motor1.clear()
        self._move_done_motor2.clear()
        self._last_final[1] = None
        self._last_final[2] = None
        self._last_residual[1] = None
        self._last_residual[2] = None

        # Motor 1 = camera arm, Motor 2 = LED arc (see models.Motor). Command each
        # motor to its own axis' target at that axis' speed.
        move_cam = self._esp.send_raw(cmd_move(1, cam_pos, cfg.camera_axis.speed_pct))
        move_led = self._esp.send_raw(cmd_move(2, led_pos, cfg.led_axis.speed_pct))

        if cfg.move_simultaneously:
            await asyncio.gather(move_cam, move_led)
        else:
            # LED arc first, then camera.
            await move_led
            await asyncio.wait_for(self._move_done_motor2.wait(), timeout=30)
            await move_cam

        # Wait for both motion-complete signals (or timeout after 30 s each)
        await asyncio.gather(
            asyncio.wait_for(self._move_done_motor1.wait(), timeout=30),
            asyncio.wait_for(self._move_done_motor2.wait(), timeout=30),
        )

        # Closed-loop refinement: nudge each motor until its output encoder reads
        # the target within tolerance. Motor 1 (camera) was commanded to cam_pos,
        # motor 2 (LED arc) to led_pos; seed the loop with the settled angle each
        # motor just reported via MOVE_DONE.
        if cfg.precise_positioning and self._settings.precise_enabled:
            r_cam, r_led = await asyncio.gather(
                motion.refine(self._esp, 1, cam_pos, self._last_final[1],
                              **self._settings.precise_params_for(1)),
                motion.refine(self._esp, 2, led_pos, self._last_final[2],
                              **self._settings.precise_params_for(2)),
            )
            self._last_residual[1] = r_cam.get("residual")
            self._last_residual[2] = r_led.get("residual")
            if not r_cam.get("encoder_ok"):
                log.warning("Precise positioning: motor 1 (camera) encoder unavailable "
                            "at CAM=%.3f° — open-loop fallback", cam_pos)
            if not r_led.get("encoder_ok"):
                log.warning("Precise positioning: motor 2 (LED arc) encoder unavailable "
                            "at LED=%.3f° — open-loop fallback", led_pos)

    def _make_filename(self, folder: str, led_pos: float, cam_pos: float,
                       led_idx: int, fmt: str) -> str:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        name = (f"LED_{led_pos:07.3f}deg_CAM_{cam_pos:07.3f}deg"
                f"_LED{led_idx:02d}_{timestamp}.{fmt}")
        return os.path.join(folder, name)

    def _make_metadata(self, led_pos: float, cam_pos: float,
                       led_idx: int, brightness: int) -> dict:
        return {
            "timestamp": datetime.now().isoformat(),
            "led_target_deg": led_pos,
            "camera_target_deg": cam_pos,
            # Physical camera-arc mounting slit (viewing elevation) for this run;
            # operator-set, constant across the scan (see ScanConfig).
            "camera_arc_angle_deg": getattr(self._config, "camera_arc_angle_deg", None),
            # True if this camera position was reached by an anti-clockwise sweep
            # (camera_target_deg is then the recorded 360−p viewing angle).
            "camera_reverse": bool(getattr(self._config, "camera_reverse", False)),
            "led_index": led_idx,
            # Fixed mounting angle of this LED on the arc (LED 1→0° … LED 7→60°),
            # distinct from led_target_deg (the arc's rotational position).
            "led_angle_deg": (LED_ARC_ANGLES_DEG[led_idx]
                              if 0 <= led_idx < len(LED_ARC_ANGLES_DEG) else None),
            "led_brightness": brightness,
            "encoder_motor1_deg": self.current_encoder.motor1_deg,
            "encoder_motor2_deg": self.current_encoder.motor2_deg,
            "encoder_led_arc_deg": self.current_encoder.led_arc_deg,
            "encoder_camera_deg": self.current_encoder.camera_deg,
            "encoder_led_error_deg": self.current_encoder.led_error_deg,
            "encoder_camera_error_deg": self.current_encoder.camera_error_deg,
            # Closed-loop residual = encoder − target after refinement (None if
            # precise positioning was off or the encoder was unavailable).
            # Motor 1 = camera, motor 2 = LED arc.
            "led_residual_deg": self._last_residual[2],
            "camera_residual_deg": self._last_residual[1],
        }

    async def _emit_progress(self):
        if self._progress_cb:
            await self._progress_cb(self._progress)
