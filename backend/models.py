from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings
from typing import Optional
from enum import IntEnum


class Motor(IntEnum):
    BOTH = 0
    LED_ARC = 1
    CAMERA = 2


class EncoderState(BaseModel):
    motor1_deg: float = 0.0
    motor2_deg: float = 0.0
    led_arc_deg: float = 0.0
    camera_deg: float = 0.0

    @property
    def led_error_deg(self) -> float:
        diff = self.led_arc_deg - self.motor1_deg
        # Normalise to [-180, 180]
        diff = (diff + 180) % 360 - 180
        return round(diff, 4)

    @property
    def camera_error_deg(self) -> float:
        diff = self.camera_deg - self.motor2_deg
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

    class Config:
        env_prefix = "GR_"
