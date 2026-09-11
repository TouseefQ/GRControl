from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings
from typing import Optional
from enum import IntEnum


class Motor(IntEnum):
    BOTH = 0
    CAMERA = 1    # Motor 1 = camera arm
    LED_ARC = 2   # Motor 2 = LED arc


class EncoderState(BaseModel):
    motor1_deg: float = 0.0           # camera motor shaft (AS5600 #1, raw)
    motor2_deg: float = 0.0           # LED motor shaft (AS5600 #2, raw)
    led_arc_deg: Optional[float] = None   # LED arc rod from home (AS5600 #3)
    camera_deg: Optional[float] = None   # camera arm from home (OME85)
    cmd_camera_deg: Optional[float] = None  # commanded camera position (from step counter)
    cmd_led_deg: Optional[float] = None     # commanded LED position (from step counter)

    @property
    def led_error_deg(self) -> Optional[float]:
        if self.led_arc_deg is None or self.cmd_led_deg is None:
            return None
        diff = self.led_arc_deg - self.cmd_led_deg
        diff = (diff + 180) % 360 - 180
        return round(diff, 4)

    @property
    def camera_error_deg(self) -> Optional[float]:
        if self.camera_deg is None or self.cmd_camera_deg is None:
            return None
        diff = self.camera_deg - self.cmd_camera_deg
        diff = (diff + 180) % 360 - 180
        return round(diff, 4)


class DeviceState(BaseModel):
    encoder: EncoderState = EncoderState()
    motor1_moving: bool = False
    motor2_moving: bool = False
    led_states: list[int] = Field(default_factory=lambda: [0] * 7)
    led_brightness: list[int] = Field(default_factory=lambda: [0] * 7)
    connected: bool = False
    connection_type: Optional[str] = None  # "serial" | "tcp"
    ts: int = 0
    dir_flip_1: bool = False
    dir_flip_2: bool = False


class ScanAxis(BaseModel):
    start_deg: float = 0.0
    stop_deg: float = 360.0
    step_deg: float = 10.0
    speed_pct: int = 80

    @property
    def positions(self) -> list[float]:
        result = []
        angle = self.start_deg
        while angle <= self.stop_deg + 1e-9:
            result.append(round(angle, 4))
            angle += self.step_deg
        return result


# Fixed mounting angle of each of the 7 LEDs on the arc: LED 1 (index 0) sits at
# 0°, spaced 10° apart, up to LED 7 (index 6) at 60°. Physical geometry — recorded
# per-image and shown in the UI. The frontend mirrors this list in app.js.
LED_ARC_ANGLES_DEG = [round(i * 10.0, 4) for i in range(7)]


class LedPattern(BaseModel):
    # Default: only L1 enabled, all at full brightness (matches the UI defaults).
    enabled: list[bool] = Field(default_factory=lambda: [i == 0 for i in range(7)])
    brightness: list[int] = Field(default_factory=lambda: [255] * 7)


class ScanConfig(BaseModel):
    led_axis: ScanAxis = ScanAxis()
    camera_axis: ScanAxis = ScanAxis()
    led_pattern: LedPattern = LedPattern()
    image_format: str = "tiff"  # tiff | png | bmp | jpeg
    output_folder: str = "."
    # Camera-arc mounting slit (viewing elevation), degrees. A physical,
    # operator-set value: the camera is bolted into one of the arc's angled
    # slits (20–55° in 5° steps), so it is constant for the whole run — not
    # motor-driven and not read from an encoder. Recorded verbatim in every
    # image's JSON sidecar.
    camera_arc_angle_deg: float = 20.0
    # Reverse (anti-clockwise) camera-arc sweep. The camera arm can only travel
    # 0–180° clockwise from home, so viewing angles in 180–360° are reached by
    # rotating anti-clockwise instead. When True, each configured camera position
    # p (entered as 0–180) is COMMANDED to the motor as −p (drives CCW) and
    # RECORDED as its mirror 360−p (0→360, 10→350, …, 180→180).
    camera_reverse: bool = False
    move_simultaneously: bool = True
    # Seconds to hold each LED lit and let the arm settle before the camera
    # captures. Was effectively 50 ms (too fast to expose); default 5 s.
    settle_s: float = 5.0
    # Closed-loop precise positioning: after the coarse move, nudge each motor
    # until its output encoder reads the target within tolerance.
    precise_positioning: bool = True
    # Optional per-scan override of the LED-arc lead margin (deg). None → use
    # Settings.camera_keepout_deg. See Settings for the rule.
    camera_keepout_deg: Optional[float] = None


class ScanProgress(BaseModel):
    running: bool = False
    paused: bool = False
    total_positions: int = 0
    current_position: int = 0
    current_led_pos_deg: float = 0.0
    current_cam_pos_deg: float = 0.0
    current_led_index: int = 0
    images_captured: int = 0
    errors: list[str] = Field(default_factory=list)


class Settings(BaseSettings):
    host: str = "0.0.0.0"
    port: int = 8000
    serial_baudrate: int = 115200
    esp32_tcp_port: int = 8888
    telemetry_interval_ms: int = 100
    command_timeout_s: float = 2.0
    ping_interval_s: float = 5.0

    # ── Closed-loop precise positioning ──────────────────────────────────────
    precise_enabled: bool = True          # master switch (per-move flags override)
    precise_gain: float = 0.8             # correction covers 80% of error → converges
    precise_max_iter: int = 6             # hard cap; report best-achieved after this
    precise_jog_speed_pct: int = 15       # slow, gentle correction nudges
    precise_tol_camera_deg: float = 0.03  # OME85 read-noise floor (camera arm)
    precise_tol_led_deg: float = 0.10     # AS5600 12-bit 0.088°/count (LED arc)

    # Motor µstep resolution = 360 / (gear_ratio * steps_per_rev). The loop won't
    # command a nudge finer than this (it can't move less than one µstep).
    steps_per_rev: float = 3200.0
    camera_gear_ratio: float = 8.0
    led_gear_ratio: float = 3.0

    # ── Camera / LED-arc occlusion guard ─────────────────────────────────────
    # The LED arc blocks the lens when it rotates AHEAD of the camera (as seen
    # from the sample). At home both axes are 0° and the arc rests at the lens
    # edge, so a position is occluded when the arc LEADS the camera by more than
    # this margin:  normalize(led − cam_view) > camera_keepout_deg. The scan
    # SKIPS those positions (does not capture them).
    #
    #   0  (default) → capture only led ≤ cam; skip led > cam  (arc in front)
    #   +m           → tolerate the arc leading by up to m° before skipping
    #   −m           → also skip within m° below the camera (more conservative)
    #
    # Directional rule, NOT a symmetric window: a pair with the arc behind the
    # camera (led < cam) is never skipped. See scan/controller.occluded_positions.
    camera_keepout_deg: float = 0.0

    # ── Camera USB bandwidth pacing ──────────────────────────────────────────
    # THE fix for the GC_ERR_TIMEOUT / incomplete-buffer stalls on this rig
    # (resolved 2026-09-11). Continuous 12-bit at full rate hit the host's USB3
    # controller with bursts it couldn't complete → torn buffers, stalls, and a
    # dead control channel needing a physical replug. Capping DeviceLinkThroughput‐
    # Limit paces the byte rate (like IDS peak Cockpit does) so 12-bit streams
    # cleanly. Applied on open, each guarded so a missing node can't break open.
    #
    # camera_throughput_limit_mbps: DeviceLinkThroughputLimit in MB/s. 100 is the
    #   confirmed-stable value on the rig (U3-34L0XCP + this laptop's xHCI). Lower
    #   it (60/40) if a different host still stalls; 0 = no limit (camera max).
    # camera_frame_rate_hz: caps AcquisitionFrameRate (0 = camera max). Not needed
    #   once throughput is paced; the max is only ~8.35 fps at 12-bit anyway.
    camera_frame_rate_hz: float = 0.0
    camera_throughput_limit_mbps: float = 100.0

    # ── Camera pixel format ──────────────────────────────────────────────────
    # Stream ONE format for the whole session — no runtime format switching,
    # which proved unreliable on this host (a switch's control writes fail and
    # wedge the link). Stream 12-bit everywhere so both preview and Save use it:
    # with the throughput pacing above, continuous 12-bit is stable, and because
    # preview and capture share the format nothing ever switches. Raw TIFF saves
    # are therefore full-depth 12-bit. The U3-34L0XCP offers BayerRG10g40IDS
    # (10-bit) and BayerRG12g24IDS (12-bit). Empty = keep the camera's power-on
    # format. Drop to 10-bit only if a host can't sustain paced 12-bit.
    camera_pixel_format: str = "BayerRG12g24IDS"

    # Optional capture-time format switch (10-bit preview / 12-bit save). DISABLED
    # ("") because we stream 12-bit for everything — no switch needed, and runtime
    # switching wedges this host. Left as a knob: set to a heavier format ONLY on
    # a host where the live stream must be lighter than the saved TIFF AND runtime
    # switching is reliable there. When set, the switch is bulletproofed (recovery
    # gated off during it; any failure restores the stream format and captures
    # there) so it can't wedge — but prefer single-format streaming.
    camera_capture_pixel_format: str = ""

    def precise_params_for(self, motor: int) -> dict:
        """Per-motor tolerance + min-step floor. motor 1 = camera, 2 = LED arc."""
        if motor == 1:
            tol = self.precise_tol_camera_deg
            gear = self.camera_gear_ratio
        else:
            tol = self.precise_tol_led_deg
            gear = self.led_gear_ratio
        min_step = 360.0 / (gear * self.steps_per_rev)
        return {
            "tol": tol,
            "gain": self.precise_gain,
            "max_iter": self.precise_max_iter,
            "jog_speed": self.precise_jog_speed_pct,
            "min_step": min_step,
        }

    class Config:
        env_prefix = "GR_"
