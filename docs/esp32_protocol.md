# ESP32 Communication Protocol Specification

**Project:** GRControlSoftware — Gonioreflectometer Control  
**Version:** 1.0  
**Date:** 2026-06-14  
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
| motor   | int   | 1 or 2  | 1 = LED arc motor, 2 = Camera motor   |
| angle   | float | 0–360   | Target absolute angle in degrees       |
| speed   | int   | 1–100   | Speed as % of maximum RPM              |

ESP32 replies with `ACK`, then sends `STATE` updates during motion, and a `MOVE_DONE` event on completion.

---

### 3.4 JOG — Jog Motor by Relative Steps

```json
{"type": "JOG", "motor": 2, "direction": 1, "steps": 50, "speed": 30}
```

| Field     | Type | Range   | Description                          |
|-----------|------|---------|--------------------------------------|
| motor     | int  | 1 or 2  | Motor to jog                         |
| direction | int  | 1 or -1 | 1 = positive (CW), -1 = negative    |
| steps     | int  | 1–10000 | Number of microsteps to move         |
| speed     | int  | 1–100   | Speed as % of maximum                |

ESP32 replies with `ACK`.

---

### 3.5 STOP — Stop Motor Immediately

```json
{"type": "STOP", "motor": 1}
```

| Field | Type | Range      | Description                        |
|-------|------|------------|------------------------------------|
| motor | int  | 0, 1, or 2 | 0 = stop both, 1 = LED, 2 = Camera |

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
  "motor1_moving": false,
  "motor2_moving": false,
  "led_states": [0, 0, 1, 0, 0, 0, 0],
  "led_brightness": [0, 0, 200, 0, 0, 0, 0]
}
```

| Field             | Type       | Description                                           |
|-------------------|------------|-------------------------------------------------------|
| ts                | uint32     | Timestamp in ms since ESP32 boot                      |
| enc_motor1_deg    | float      | AS5600 on Motor 1 shaft (LED arc motor)               |
| enc_motor2_deg    | float      | AS5600 on Motor 2 shaft (Camera motor)                |
| enc_led_arc_deg   | float      | AS5600 on LED arc vertical shaft (final output)       |
| enc_camera_deg    | float      | AksIM-4 on camera gear (final output)                 |
| motor1_moving     | bool       | True if motor 1 is currently stepping                 |
| motor2_moving     | bool       | True if motor 2 is currently stepping                 |
| led_states        | int[7]     | Current on/off state of each LED                      |
| led_brightness    | int[7]     | Current brightness of each LED                        |

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
| 2    | Encoder read failure (AksIM-4) |
| 3    | I2C bus error                  |
| 4    | SPI bus error                  |
| 5    | Motor driver fault             |
| 6    | JSON parse error               |

---

## 5. Encoder Reading Notes

### AS5600 (I2C, address 0x36 and 0x37)

- Read registers `0x0C` (high byte) and `0x0D` (low byte) for 12-bit raw angle.
- Raw value range: 0–4095 → convert to degrees: `angle = raw * 360.0 / 4096.0`
- Apply home offset: `angle_deg = fmod(raw_deg - home_offset + 360.0, 360.0)`
- If two AS5600s share the same I2C bus, use an I2C multiplexer (TCA9548A) or use separate I2C buses on the ESP32 (ESP32 supports 2 hardware I2C peripherals).

### AksIM-4 (BiSS-C over SPI)

- SPI Mode 0, CPOL=0, CPHA=0
- Clock frequency: max 10 MHz (recommend 1–4 MHz for reliability)
- Frame: `[1 start bit][1 CDS bit][18 position bits][2 status bits][6 CRC bits]`
- Total frame: 28 bits minimum — read 4 bytes (32 bits), discard first 2 bits
- Position bits are **MSB first**, **left-aligned**
- Status bits: bit[1]=`nError` (active low), bit[0]=`nWarn` (active low)
- CRC polynomial: `x^6 + x^1 + 1` (0x43), initial value 0x3F, inverted
- Raw angle conversion: `angle = raw_18bit * 360.0 / 262144.0`
- Apply home offset same as AS5600.

```c
// Minimal BiSS-C read sketch (ESP32 Arduino)
uint32_t readAksIM4() {
    uint8_t buf[4] = {0};
    digitalWrite(CS_PIN, LOW);
    delayMicroseconds(1);
    SPI.transferBytes(NULL, buf, 4);
    digitalWrite(CS_PIN, HIGH);

    // Assemble 32-bit word, skip first 2 framing bits
    uint32_t raw = ((uint32_t)buf[0] << 24) | ((uint32_t)buf[1] << 16)
                 | ((uint32_t)buf[2] << 8)  | buf[3];
    uint32_t position = (raw >> 12) & 0x3FFFF; // 18-bit position
    uint8_t  status   = (raw >> 10) & 0x03;
    uint8_t  crc      = (raw >> 4)  & 0x3F;
    // TODO: validate CRC before trusting position
    return position;
}
```

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
