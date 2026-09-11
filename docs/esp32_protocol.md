# ESP32 Communication Protocol Specification

**Project:** GRControlSoftware — Gonioreflectometer Control  
**Version:** 1.1  
**Date:** 2026-09-10  
**Interface:** USB/Serial (115200 baud) and WiFi TCP (port 8888)

---

## 1. Overview

All messages are UTF-8 encoded JSON objects terminated by a single newline character `\n`.  
The PC acts as the **master**. The ESP32 acts as the **slave**, responding to commands and sending periodic telemetry.

```
PC  ──(command)\n──►  ESP32
ESP32 ──(response/telemetry)\n──►  PC
```

Both Serial and TCP transports use identical message formats.

---

## 2. General Frame Structure

Every message is a single JSON object on one line:

```json
{"type": "MSG_TYPE", ...fields}
```

- **`type`** is always present and indicates message kind.
- All angle values are in **degrees** (float, 4 decimal places).
- All timestamps are **milliseconds since ESP32 boot** (uint32).

---

## 3. PC → ESP32 Commands

### 3.1 PING — Connection Check

```json
{"type": "PING"}
```

ESP32 replies with `PONG`.

---

### 3.2 GET_STATE — Request Full State

```json
{"type": "GET_STATE"}
```

ESP32 replies immediately with a `STATE` message (see Section 4.1).

---

### 3.3 MOVE — Move Motor to Absolute Angle

```json
{"type": "MOVE", "motor": 1, "angle": 45.0, "speed": 80}
```

| Field   | Type  | Range   | Description                            |
|---------|-------|---------|----------------------------------------|
| motor   | int   | 0, 1, 2 | 0 = both, 1 = Camera motor, 2 = LED arc motor |
| angle   | float | absolute | Target absolute output-shaft angle in degrees. Negative or >360 values are accepted (linear absolute target) — e.g. −10 drives the arm anti-clockwise, used for the reverse camera-arc sweep |
| speed   | int   | 1–100   | Speed as % of maximum RPM              |

ESP32 replies with `ACK`, then sends `STATE` updates during motion, and a `MOVE_DONE` event on completion.

---

### 3.4 JOG — Jog Motor by Relative Degrees

```json
{"type": "JOG", "motor": 1, "direction": 1, "degrees": 5.0, "speed": 30}
```

| Field     | Type  | Range   | Description                          |
|-----------|-------|---------|--------------------------------------|
| motor     | int   | 1 or 2  | Motor to jog                         |
| direction | int   | 1 or -1 | 1 = positive (CW), -1 = negative (CCW) |
| degrees   | float | >0      | Output-shaft degrees to move         |
| speed     | int   | 1–100   | Speed as % of maximum                |

ESP32 replies with `ACK`.

---

### 3.5 STOP — Stop Motor Immediately

```json
{"type": "STOP", "motor": 1}
```

| Field | Type | Range      | Description                        |
|-------|------|------------|------------------------------------|
| motor | int  | 0, 1, or 2 | 0 = stop both, 1 = Camera, 2 = LED arc |

ESP32 replies with `ACK`.

---

### 3.6 SET_HOME — Set Current Position as Zero Reference

```json
{"type": "SET_HOME", "motor": 0}
```

| Field | Type | Range      | Description                         |
|-------|------|------------|-------------------------------------|
| motor | int  | 0, 1, or 2 | 0 = set home for both motors        |

Sets the current encoder reading as the 0° reference. All subsequent angle reports are relative to this reference. ESP32 replies with `ACK`.

---

### 3.7 LED_SET — Control Individual LED

```json
{"type": "LED_SET", "index": 2, "state": 1, "brightness": 200}
```

| Field      | Type | Range  | Description                           |
|------------|------|--------|---------------------------------------|
| index      | int  | 0–6    | LED index (0 = first LED on strip)    |
| state      | int  | 0 or 1 | 0 = off, 1 = on                       |
| brightness | int  | 0–255  | PWM brightness value (PCA9685 scale)  |

ESP32 replies with `ACK`.

---

### 3.8 LED_ALL — Set All LEDs at Once

```json
{"type": "LED_ALL", "states": [0,0,1,0,0,0,0], "brightness": [0,0,200,0,0,0,0]}
```

| Field      | Type      | Length | Description                          |
|------------|-----------|--------|--------------------------------------|
| states     | int array | 7      | On/off per LED                        |
| brightness | int array | 7      | Brightness per LED (0–255)            |

ESP32 replies with `ACK`.

---

### 3.9 LED_OFF_ALL — Turn Off All LEDs

```json
{"type": "LED_OFF_ALL"}
```

Turns all 7 LEDs off immediately. ESP32 replies with `ACK`.

---

### 3.10 SET_TELEMETRY — Configure Telemetry Rate

```json
{"type": "SET_TELEMETRY", "interval_ms": 100}
```

| Field       | Type | Range    | Description                    |
|-------------|------|----------|--------------------------------|
| interval_ms | int  | 50–5000  | Telemetry send interval in ms  |

Default is 100 ms (10 Hz). ESP32 replies with `ACK`.

---

### 3.11 SET_CONFIG — Persist Motor Configuration

```json
{"type": "SET_CONFIG", "dir_flip_1": true, "dir_flip_2": false, "max_speed_sps": 10000, "motor_idle_ms": 3000}
```

| Field         | Type   | Description                                                       |
|---------------|--------|-------------------------------------------------------------------|
| dir_flip_1    | bool   | (optional) Invert Motor 1 (camera) rotation direction             |
| dir_flip_2    | bool   | (optional) Invert Motor 2 (LED arc) rotation direction            |
| max_speed_sps | float  | (optional) Max step rate in steps/s                               |
| motor_idle_ms | uint32 | (optional) Idle time before the drivers auto-disable (0 = never)  |

Only the fields present are applied; each is persisted to NVS so it survives a reboot. ESP32 replies with `ACK`.

---

## 4. ESP32 → PC Messages

### 4.1 STATE — Periodic Telemetry

Sent automatically at the configured telemetry interval (default 100 ms).

```json
{
  "type": "STATE",
  "ts": 123456,
  "enc_motor1_deg": 10.0021,
  "enc_motor2_deg": 9.9987,
  "enc_led_arc_deg": 10.4512,
  "enc_camera_deg": 10.0103,
  "cmd_camera_deg": 10.0,
  "cmd_led_deg": 10.0,
  "motor1_moving": false,
  "motor2_moving": false,
  "led_states": [0, 0, 1, 0, 0, 0, 0],
  "led_brightness": [0, 0, 200, 0, 0, 0, 0],
  "dir_flip_1": false,
  "dir_flip_2": false,
  "motor_idle_ms": 3000
}
```

| Field             | Type          | Description                                                     |
|-------------------|---------------|-----------------------------------------------------------------|
| ts                | uint32        | Timestamp in ms since ESP32 boot                                |
| enc_motor1_deg    | float         | AS5600 #1 on Motor 1 (camera) shaft; home-relative, signed (−180, 180] |
| enc_motor2_deg    | float         | AS5600 #2 on Motor 2 (LED arc) shaft; home-relative, signed (−180, 180] |
| enc_led_arc_deg   | float \| null | AS5600 #3 on LED arc output shaft (final); home-relative, signed (−180, 180], sign-corrected to the motor; `null` if read failed |
| enc_camera_deg    | float \| null | OME85 on camera output gear (final); home-relative, signed (−180, 180], sign-corrected to the motor; `null` if read failed      |
| cmd_camera_deg    | float         | Commanded camera angle (from the step counter)                  |
| cmd_led_deg       | float         | Commanded LED-arc angle (from the step counter)                 |
| motor1_moving     | bool          | True if motor 1 (camera) is currently stepping                  |
| motor2_moving     | bool          | True if motor 2 (LED arc) is currently stepping                 |
| led_states        | int[7]        | Current on/off state of each LED                                |
| led_brightness    | int[7]        | Current brightness of each LED                                  |
| dir_flip_1        | bool          | Current direction-inversion flag for Motor 1 (camera)           |
| dir_flip_2        | bool          | Current direction-inversion flag for Motor 2 (LED arc)          |
| motor_idle_ms     | uint32        | Idle timeout (ms) before the stepper drivers are disabled       |

---

### 4.2 ACK — Command Acknowledged

```json
{"type": "ACK", "cmd": "MOVE", "ts": 123460}
```

Sent in response to every command. Confirms the ESP32 received and accepted the command.

---

### 4.3 NACK — Command Rejected

```json
{"type": "NACK", "cmd": "MOVE", "reason": "Motor already moving", "ts": 123461}
```

Sent when a command cannot be executed (e.g., move requested while already moving).

---

### 4.4 PONG — Ping Response

```json
{"type": "PONG", "ts": 123462}
```

---

### 4.5 MOVE_DONE — Motion Complete

```json
{"type": "MOVE_DONE", "motor": 1, "final_angle": 45.0023, "ts": 123999}
```

Sent when a `MOVE` command completes (motor has reached target and stopped).

---

### 4.6 ERROR — Hardware or Firmware Error

```json
{"type": "ERROR", "code": 3, "msg": "I2C timeout on AS5600 (addr 0x36)", "ts": 124000}
```

| Code | Meaning                        |
|------|--------------------------------|
| 1    | Encoder read failure (AS5600)  |
| 2    | Encoder read failure (OME85)   |
| 3    | I2C bus error                  |
| 4    | SPI bus error                  |
| 5    | Motor driver fault             |
| 6    | JSON parse error               |

---

## 5. Encoder Reading Notes

**Home-relative angle convention (firmware `homeRelSigned`).** All `enc_*` values
are reported as `raw − home` **wrapped into (−180, 180]**, not `[0, 360)`. This
keeps a homed axis (0°) in the *middle* of the range instead of on the 0/360
seam — otherwise encoder read-noise dithers a homed axis 0.005 ↔ 359.995 (visible
flicker) and, worse, makes the step-counter resync (`initStepCounters`) seed ~360°
so the next absolute `MOVE` sweeps a near-full turn.

**Output-encoder sign inversion.** The two *output-shaft* encoders — OME85
(camera) and AS5600 #3 (LED arc) — sit after a single gear mesh, so they rotate
**opposite** to their motor. Their reported angle is **negated** so a `+` command
reads `+` and agrees with the step counter and the closed-loop refinement. The two
*motor-shaft* encoders (AS5600 #1/#2) are not inverted.

### AS5600 (I2C, address 0x36 and 0x37)

- Read registers `0x0C` (high byte) and `0x0D` (low byte) for 12-bit raw angle.
- Raw value range: 0–4095 → convert to degrees: `angle = raw * 360.0 / 4096.0`
- Apply home offset, signed: `angle_deg = homeRelSigned(raw_deg, home_offset, reversed)` — wrap `(raw_deg − home)` (negated for the output encoder) into (−180, 180].
- If two AS5600s share the same I2C bus, use an I2C multiplexer (TCA9548A) or use separate I2C buses on the ESP32 (ESP32 supports 2 hardware I2C peripherals).

### OME85 (BiSS-C over SPI, camera output gear)

The camera arm's final-output encoder is an **OTV Sensing OME85** absolute
encoder, read over a BiSS-C interface on the SPI pins (via an RS422 transceiver).
*(This replaces the AksIM-4 named in earlier revisions — that part is not used.)*

- BiSS-C clock; the OME85 requires **≥ 500 kHz**. CLK is driven directly; the
  DATA line returns through the RS422 receiver.
- The frame carries a **17-bit position field** plus error/warning and CRC bits.
- **CRC is currently unusable on this PCB:** the DATA line is routed through a
  TXS0108E auto-direction level shifter whose edge accelerators corrupt the
  trailing (EW + CRC) bits at 500 kHz. The **position field is reliable**; CRC/EW
  are ignored until the DATA path is fixed (e.g. a 3.3 V MAX3490, or a divider).
- **Workaround in firmware:** sample the position 15× and take the **majority
  vote** (readings within 2 counts fold together to absorb LSB dither); the stuck
  top bit is masked so the field is used as 16 bits.
- Angle conversion: `angle = (position & 0xFFFF) / 65536.0 * 360.0`
- Apply the home offset, signed and sign-corrected (camera output is reversed): `angle_deg = homeRelSigned(angle, home, true)` — wrap `−(angle − home)` into (−180, 180].
- The read is **blocking (~4 ms)** and only valid while the camera motor is
  stationary.

See `ArduinoCode/OME85-Reader/OME85-Reader.ino` and the `ome85_*` functions in
`GRControl-Full.ino` for the reference implementation.

---

## 6. Error Handling Rules

1. If the ESP32 receives malformed JSON, reply with `NACK` and error code 6.
2. If an encoder read fails 3 consecutive times, send an `ERROR` message and halt motion.
3. The PC must send a `PING` every 5 seconds. If the ESP32 receives no message for 10 seconds, it should stop all motors and turn off all LEDs (watchdog).
4. The PC implements a 2-second command timeout: if no `ACK` is received within 2 seconds, the command is retried once, then an error is raised to the operator.

---

## 7. Typical Scan Sequence (Reference)

```
PC: {"type": "MOVE", "motor": 1, "angle": 45.0, "speed": 80}
PC: {"type": "MOVE", "motor": 2, "angle": 30.0, "speed": 80}
ESP32: {"type": "ACK", "cmd": "MOVE", ...}  (x2)
ESP32: [STATE updates every 100ms during motion]
ESP32: {"type": "MOVE_DONE", "motor": 1, "final_angle": 45.0021, ...}
ESP32: {"type": "MOVE_DONE", "motor": 2, "final_angle": 30.0003, ...}

PC: {"type": "LED_SET", "index": 0, "state": 1, "brightness": 200}
[PC triggers camera capture]
PC: {"type": "LED_SET", "index": 0, "state": 0, "brightness": 0}

PC: {"type": "LED_SET", "index": 1, "state": 1, "brightness": 200}
[PC triggers camera capture]
PC: {"type": "LED_SET", "index": 1, "state": 0, "brightness": 0}

[... repeat for all active LEDs ...]

PC: {"type": "MOVE", "motor": 1, "angle": 50.0, "speed": 80}
[... next scan position ...]
```
