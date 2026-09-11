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
import json
import logging
import os
import re
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


def _abs360(a: Optional[float]) -> Optional[float]:
    """Absolute angle in [0, 360). A signed/negative angle maps to its positive
    equivalent (−10 → 350, −180 → 180, −90 → 270); positives are unchanged. Used
    for RECORDED angles (sidecar + filename + progress) so they report the
    absolute viewing angle regardless of which way the axis was swept. The motor
    is still COMMANDED the signed value, so it sweeps the intended direction."""
    return None if a is None else round(a % 360.0, 4)


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
        """Effective max LED-arc lead angle that blocks the lens (deg) for this
        run: the per-scan override if set, else the workspace default. See
        occluded_positions. 0 (or negative) disables the guard."""
        ko = config.camera_keepout_deg
        return self._settings.camera_keepout_deg if ko is None else ko

    def occluded_positions(self, config: ScanConfig) -> list[tuple[float, float]]:
        """Every (led_pos, cam_pos) grid pair where the LED arc sits in front of
        the lens, so it must be skipped.

        At home (both 0°) the LED arc rests at the edge of the lens (clear). As the
        arc rotates AHEAD of the camera it moves into the lens's view and blocks it
        — but only up to a point: once it leads far enough it swings clear again.
        Measured on the rig: the arc blocks while it leads the camera by 0–50°, and
        is clear beyond that. So a pair is occluded within that one-sided window:

            0 < normalize(led − cam) <= camera_keepout_deg

        camera_keepout_deg is the max lead angle that still blocks (default 50).
        The arc level with or behind the camera (lead <= 0) is always clear, and so
        is the arc led past the window (lead > keepout). keepout <= 0 disables the
        guard entirely. Angles are the configured values directly (a negative
        camera angle simply sweeps the other way)."""
        keepout = self.keepout_deg(config)
        bad = []
        for led_pos in config.led_axis.positions:
            for cam_pos in config.camera_axis.positions:
                lead = motion.normalize_deg(led_pos - cam_pos)
                if 0.0 < lead <= keepout:
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
        scan_start = datetime.now()
        led_positions = cfg.led_axis.positions
        cam_positions = cfg.camera_axis.positions
        active_leds = [i for i, en in enumerate(cfg.led_pattern.enabled) if en]

        # Positions where the LED arc would block the lens are skipped, not
        # scanned. Size the total to only the pairs we will actually capture.
        occluded = set(self.occluded_positions(cfg))
        scanned_pairs = len(led_positions) * len(cam_positions) - len(occluded)
        repeats = max(1, int(getattr(cfg, "repeats_per_led", 1)))
        total = scanned_pairs * len(active_leds) * repeats
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

        # Initialized here so the finally block can always reference them.
        cam_info: dict = {}
        scan_cfg_dict: dict = {}
        _scan_records: dict = {}

        try:
            # Snapshot camera settings (exposure, gain, format, …) once for the
            # scan-level JSON. The camera module already reads these at capture time
            # but collecting them here keeps them in one place at scan start.
            if self._camera and hasattr(self._camera, "get_info"):
                try:
                    cam_info = await self._camera.get_info() or {}
                except Exception as _e:
                    log.warning("Could not read camera info for scan JSON: %s", _e)
            pf = cam_info.get("pixel_format") or ""
            _m = re.match(r"Bayer[RGB]{2}(\d+)", pf)
            if _m:
                cam_info["bit_depth"] = int(_m.group(1))

            scan_cfg_dict = cfg.model_dump()
            scan_cfg_dict["led_positions"] = cfg.led_axis.positions
            scan_cfg_dict["camera_positions"] = cfg.camera_axis.positions

            # Camera is the OUTER axis: it holds a position while the LED arc
            # sweeps 0..(camera angle), then the camera advances and the arc
            # resets to 0. This matches the physical scan and the occlusion rule
            # (the arc is skipped wherever it would lead the camera).
            for cam_pos in cam_positions:
                for led_pos in led_positions:
                    if (led_pos, cam_pos) in occluded:
                        continue  # LED arc leads the camera here — skip

                    # Pause check
                    while self._progress.paused:
                        await asyncio.sleep(0.1)

                    # Resolve the LED-arc motor target. In relative mode the
                    # configured led_pos is an OFFSET from the camera: the arc is
                    # commanded to camera_pos + offset so the geometry tracks the
                    # camera. Otherwise led_pos is the absolute arc angle.
                    led_cmd = (cam_pos + led_pos) if cfg.led_relative else led_pos
                    led_offset = led_pos if cfg.led_relative else None

                    # Move motors (camera to cam_pos, LED arc to led_cmd). A
                    # negative angle simply sweeps the arm the other way (firmware
                    # MOVE is absolute).
                    await self._move_to(led_cmd, cam_pos, cfg)

                    # Per-LED capture. The LED is lit once, held while the arm
                    # settles, then captured `repeats` times, then turned off —
                    # so all repeats of one LED share a single on/off cycle.
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

                        for rep in range(1, repeats + 1):
                            while self._progress.paused:
                                await asyncio.sleep(0.1)

                            # Capture
                            filename = self._make_filename(
                                cfg.output_folder, led_cmd, cam_pos, led_idx,
                                cfg.image_format, rep, repeats
                            )
                            metadata = self._make_metadata(
                                led_cmd, cam_pos, led_idx, brightness,
                                rep, repeats, led_offset
                            )
                            saved_path = await self._camera.capture(
                                filename, cfg.image_format, metadata
                            )
                            # Store per-image metadata in the scan record. The camera
                            # module mutates `metadata` in-place (adds pixel_format,
                            # raw_bayer, bit_depth, preview_png for TIFF), so reading
                            # it here captures those camera-level fields too.
                            img_key = os.path.basename(filename)
                            _scan_records[img_key] = metadata
                            if not saved_path:
                                metadata["capture_failed"] = True

                            if saved_path:
                                self._progress.images_captured += 1
                                if self._image_cb:
                                    preview = await self._camera.grab_preview_jpeg()
                                    if preview:
                                        await self._image_cb(saved_path, preview)
                            else:
                                self._progress.errors.append(
                                    f"Capture failed at LED={led_cmd}° CAM={cam_pos}° "
                                    f"LED#{led_idx} rep{rep}"
                                )

                            self._progress.current_position += 1
                            self._progress.current_led_pos_deg = _abs360(led_cmd)
                            self._progress.current_cam_pos_deg = _abs360(cam_pos)
                            self._progress.current_led_index = led_idx
                            await self._emit_progress()

                        # Turn LED off after all repeats
                        await self._esp.send_raw(cmd_led_set(led_idx, 0, 0))

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
            # Write a single scan-level JSON: settings once + all per-image records.
            scan_json = os.path.join(
                cfg.output_folder,
                f"scan_{scan_start.strftime('%Y%m%d_%H%M%S')}.json",
            )
            try:
                with open(scan_json, "w") as _f:
                    json.dump(
                        {
                            "scan_start_time": scan_start.isoformat(),
                            "camera_settings": cam_info,
                            "scan_config": scan_cfg_dict,
                            "images": _scan_records,
                        },
                        _f,
                        indent=2,
                    )
                log.info("Scan JSON written: %s", scan_json)
            except Exception as e:
                log.error("Scan JSON write failed: %s", e)

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
                       led_idx: int, fmt: str, rep: int = 1,
                       total_reps: int = 1) -> str:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        # Absolute [0,360) angles in the name so a −10 sweep reads CAM_350.000.
        # A repeat suffix (_rep02, …) is only added when more than one image is
        # taken per LED, so single-shot scan filenames are unchanged.
        rep_tag = f"_rep{rep:02d}" if total_reps > 1 else ""
        name = (f"LED_{_abs360(led_pos):07.3f}deg_CAM_{_abs360(cam_pos):07.3f}deg"
                f"_LED{led_idx:02d}{rep_tag}_{timestamp}.{fmt}")
        return os.path.join(folder, name)

    def _make_metadata(self, led_pos: float, cam_pos: float,
                       led_idx: int, brightness: int, rep: int = 1,
                       total_reps: int = 1,
                       led_offset: Optional[float] = None) -> dict:
        return {
            "timestamp": datetime.now().isoformat(),
            # Recorded angles are absolute [0,360): a −10 sweep is recorded as 350.
            "led_target_deg": _abs360(led_pos),
            "camera_target_deg": _abs360(cam_pos),
            # In relative mode, the offset the LED arc was placed at ahead of the
            # camera (led_target_deg = camera_target_deg + this). None if the run
            # used absolute LED angles.
            "led_relative_offset_deg": (round(led_offset, 4)
                                        if led_offset is not None else None),
            # Which repeat this image is (1-based) and how many per LED.
            "repeat_index": rep,
            "repeats_total": total_reps,
            # Physical camera-arc mounting slit (viewing elevation) for this run;
            # operator-set, constant across the scan (see ScanConfig).
            "camera_arc_angle_deg": getattr(self._config, "camera_arc_angle_deg", None),
            "led_index": led_idx,
            # Fixed mounting angle of this LED on the arc (LED 1→0° … LED 7→60°),
            # distinct from led_target_deg (the arc's rotational position).
            "led_angle_deg": (LED_ARC_ANGLES_DEG[led_idx]
                              if 0 <= led_idx < len(LED_ARC_ANGLES_DEG) else None),
            "led_brightness": brightness,
            # Measured absolute positions, [0,360).
            "encoder_motor1_deg": _abs360(self.current_encoder.motor1_deg),
            "encoder_motor2_deg": _abs360(self.current_encoder.motor2_deg),
            "encoder_led_arc_deg": _abs360(self.current_encoder.led_arc_deg),
            "encoder_camera_deg": _abs360(self.current_encoder.camera_deg),
            # Errors/residuals are signed deltas (target − encoder), left in
            # (−180,180] — not absolute angles.
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
