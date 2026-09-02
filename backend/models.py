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


class LedPattern(BaseModel):
    enabled: list[bool] = Field(default_factory=lambda: [True] * 7)
    brightness: list[int] = Field(default_factory=lambda: [200] * 7)


class ScanConfig(BaseModel):
    led_axis: ScanAxis = ScanAxis()
    camera_axis: ScanAxis = ScanAxis()
    led_pattern: LedPattern = LedPattern()
    image_format: str = "tiff"  # tiff | png | bmp | jpeg
    output_folder: str = "."
    move_simultaneously: bool = True
    # Seconds to hold each LED lit and let the arm settle before the camera
    # captures. Was effectively 50 ms (too fast to expose); default 5 s.
    settle_s: float = 5.0
    # Closed-loop precise positioning: after the coarse move, nudge each motor
    # until its output encoder reads the target within tolerance.
    precise_positioning: bool = True
    # Optional per-scan override of the camera keep-out half-angle (deg). None →
    # use Settings.camera_keepout_deg. See Settings for what it guards against.
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
    # The LED arc blocks the lens's view of the sample when the two axes are
    # angularly close, as seen from the sample. A scan position (led, cam) is
    # occluded when |normalize(cam − led)| < camera_keepout_deg. The scan refuses
    # to start if any grid position falls inside this window.
    #
    # The value is rig geometry: ~ atan(r_lens / R_cam) + atan((w_arc/2) / R_led),
    # with r_lens = 2.25 cm (4.5 cm lens). Best set from an empirical sweep (park
    # the camera, sweep the arc through it in the live preview, note the blocked
    # span, halve it, add margin). DEFAULT 0.0 = guard DISABLED — set your
    # calibrated half-angle here or via GR_camera_keepout_deg to turn it on.
    camera_keepout_deg: float = 0.0

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
