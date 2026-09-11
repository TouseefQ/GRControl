/*
 * GRControl-Full.ino — v2.0
 *
 * Gonioreflectometer controller for ESP32-S3
 *
 * ── Hardware assignments ──────────────────────────────────────────────────────
 * Motor 1  Camera arm   gear 8:1   STEP=21  DIR=47  EN=48
 * Motor 2  LED arc      gear 3:1   STEP=38  DIR=2   EN=1
 * Wire  I2C0  SDA=10  SCL=11       AS5600 #1 (camera motor shaft)
 * Wire1 I2C1  SDA=4   SCL=5        AS5600 #3 (LED arc rod, output encoder)
 * SoftI2C     SDA=7   SCL=15       AS5600 #2 (LED motor shaft) + PCA9685 @ 0x40
 * OME85 BiSS-C          CLK=18  DATA=17
 *
 * ── Protocol ─────────────────────────────────────────────────────────────────
 * JSON-newline over USB Serial (115200 baud) and WiFi TCP (port 8888).
 * Commands: PING, GET_STATE, MOVE, JOG (field: degrees), STOP, SET_HOME,
 *           LED_SET, LED_ALL, LED_OFF_ALL, SET_TELEMETRY, SET_CONFIG
 */

#include <Arduino.h>
#include <WiFi.h>
#include <Wire.h>
#include <AccelStepper.h>
#include <ArduinoJson.h>
#include <Preferences.h>
#include "soc/gpio_reg.h"

// ── WiFi ──────────────────────────────────────────────────────────────────────
const char*    WIFI_SSID = "Dax Pixel";
const char*    WIFI_PASS = "tpjo9963";
const uint16_t TCP_PORT  = 8888;

// ── Motor / step constants ────────────────────────────────────────────────────
#define STEPS_PER_REV      3200.0f   // 200 full steps × 16 microsteps
#define CAMERA_GEAR_RATIO   8.0f     // 14-tooth pulley → 112-tooth gear
#define LED_GEAR_RATIO      3.0f     // 20-tooth pulley → 60-tooth gear
// Mechanical direction bake: the two arcs are wired/geared so a +command turns
// them OPPOSITE ways. Invert Motor 2 (LED arc) by default so both share a +
// direction (camera kept as-is). User dir_flip_* (SET_CONFIG/NVS) toggles ON TOP.
#define M1_DIR_INVERT  false
#define M2_DIR_INVERT  true
// 200 RPM × 3200 steps/rev ÷ 60 s ≈ 10,667 steps/s  (safe for 17HS19-2004S1 @ 24 V)
#define DEFAULT_MAX_SPS    10667.0f
#define DEFAULT_ACCEL_SPS2 20000.0f
// Auto-disable the stepper drivers after this many ms of no motion, to stop the
// holding-current chopper buzz. Runtime-adjustable via SET_CONFIG (motor_idle_ms)
// and persisted in NVS. Position is recovered from the absolute encoders on wake.
#define DEFAULT_MOTOR_IDLE_MS 15000
#define MOTOR_IDLE_MS_MIN       250
#define MOTOR_IDLE_MS_MAX    600000

// ── Motor pins ────────────────────────────────────────────────────────────────
#define M1_STEP  21
#define M1_DIR   47
#define M1_EN    48
#define M2_STEP  38
#define M2_DIR    2
#define M2_EN     1

// ── I2C pin assignments ───────────────────────────────────────────────────────
#define I2C0_SDA 10
#define I2C0_SCL 11
#define I2C1_SDA  4
#define I2C1_SCL  5
#define SWIRE_SDA  7
#define SWIRE_SCL 15

// ── OME85 BiSS-C (MAX490 RS422 + TXS0108E level shifter) ─────────────────────
#define OME85_CLK  18
#define OME85_DATA 17

// Direct-register bit-bang (GPIO 17/18 are <32 → single register bank).
#define OME85_CLK_HIGH()  REG_WRITE(GPIO_OUT_W1TS_REG, (1UL << OME85_CLK))
#define OME85_CLK_LOW()   REG_WRITE(GPIO_OUT_W1TC_REG, (1UL << OME85_CLK))
#define OME85_READ_DATA() ((REG_READ(GPIO_IN_REG) >> OME85_DATA) & 1UL)

// Bit timing validated on this hardware: ~545 kHz (above OME85's 500 kHz min).
inline void biss_clockDelay()  { for (int n = 0; n < 40; n++) __asm__("nop;"); }
inline void biss_sampleDelay() { for (int n = 0; n < 30; n++) __asm__("nop;"); }

// ── AS5600 I2C constants ──────────────────────────────────────────────────────
#define AS5600_ADDR   0x36
#define AS5600_RAW_HI 0x0C

// ── PCA9685 (software I2C @ 0x40) ────────────────────────────────────────────
#define PCA9685_ADDR      0x40
#define PCA9685_MODE1     0x00
#define PCA9685_MODE2     0x01
#define PCA9685_PRESCALE  0xFE
#define PCA9685_LED0_ON_L 0x06
#define LED_COUNT         7

// ── BiSS-C frame constants ────────────────────────────────────────────────────
#define BISS_DATA_BITS 17
#define BISS_CRC_POLY  0x43
#define OME85_SAMPLES  15    // majority-vote reads per angle (CRC unusable on this PCB)

// ══════════════════════════════════════════════════════════════════════════════
// Angle helper
// ══════════════════════════════════════════════════════════════════════════════

// Home-relative angle, wrapped into (−180, 180], with optional sign inversion.
//
// Two things this fixes vs the old `fmod(raw − home + 360, 360)` → [0, 360):
//  1) SEAM: a homed axis sits at 0. In [0,360) that is the 0/360 seam, so encoder
//     read-noise dithers it 0.005 ↔ 359.995 — the flicker, and (worse) it makes
//     initStepCounters() reseed the step counter to ~360°, so the next absolute
//     MOVE sweeps a near-full turn. Wrapping to (−180,180] puts 0 in the MIDDLE
//     of the range, so noise reads ∓0.005 and never crosses a discontinuity.
//  2) SIGN: the OME85 (camera) and AS5600 #3 (LED arc) sit on the OUTPUT shaft,
//     which turns OPPOSITE the motor (single gear mesh), so a +command reads −.
//     `reversed=true` negates them so command, step counter, and encoder all
//     agree (+5° command → +5° reading). Motor-shaft encoders pass reversed=false.
float homeRelSigned(float raw, float homeOffset, bool reversed) {
  float d = raw - homeOffset;
  if (reversed) d = -d;
  d = fmodf(d + 180.0f, 360.0f);
  if (d < 0.0f) d += 360.0f;
  d -= 180.0f;
  return roundf(d * 10000.0f) / 10000.0f;
}

// ══════════════════════════════════════════════════════════════════════════════
// Globals
// ══════════════════════════════════════════════════════════════════════════════

Preferences  prefs;
AccelStepper motor1(AccelStepper::DRIVER, M1_STEP, M1_DIR);
AccelStepper motor2(AccelStepper::DRIVER, M2_STEP, M2_DIR);
WiFiServer   server(TCP_PORT);
WiFiClient   tcpClient;
String       serialBuf;
String       tcpBuf;

uint8_t  ledState[LED_COUNT]      = {};
uint16_t ledBrightness[LED_COUNT] = {};
bool     motor1Moving = false;
bool     motor2Moving = false;
bool     motorsEnabled = false;         // driver EN state (both motors together)
unsigned long lastMotorActiveMs = 0;    // last time either motor was moving

float    homeOffsetCamera = 0.0f;  // raw OME85 angle (°) at home position
float    homeOffsetLed    = 0.0f;  // raw AS5600 #3 angle (°) at home position
float    homeOffsetMotor1 = 0.0f;  // raw AS5600 #1 (camera motor shaft) at home
float    homeOffsetMotor2 = 0.0f;  // raw AS5600 #2 (LED motor shaft) at home
float    lastCameraDeg    = 0.0f;  // last OME85 read (cached; updated only at rest)
bool     cameraValid      = false; // was the last OME85 read successful?
bool     dir_flip_1       = false;
bool     dir_flip_2       = false;
float    maxSpeedSPS      = DEFAULT_MAX_SPS;
uint32_t motorIdleMs      = DEFAULT_MOTOR_IDLE_MS;  // idle-disable timeout (ms)

unsigned long lastMsgMs       = 0;
unsigned long lastTelemetryMs = 0;
unsigned long lastPcaCheckMs  = 0;
uint16_t      telemetryInterval = 100;

// ── Direction / encoder-sign helpers ─────────────────────────────────────────
// Effective motor-direction inversion = mechanical bake XOR user dir_flip.
inline bool m1Inverted() { return M1_DIR_INVERT ^ dir_flip_1; }
inline bool m2Inverted() { return M2_DIR_INVERT ^ dir_flip_2; }
// Effective encoder "reversed" flag for homeRelSigned(). Base: output-shaft
// encoders (OME85, AS5600 #3) read opposite their motor (single gear mesh) →
// true; motor-shaft encoders (AS5600 #1/#2) → false. XOR the motor's effective
// inversion so +command always reads +angle regardless of direction flips.
inline bool camOutRev()  { return true  ^ m1Inverted(); }  // OME85 (camera output)
inline bool ledOutRev()  { return true  ^ m2Inverted(); }  // AS5600 #3 (LED arc output)
inline bool m1ShaftRev() { return false ^ m1Inverted(); }  // AS5600 #1 (camera shaft)
inline bool m2ShaftRev() { return false ^ m2Inverted(); }  // AS5600 #2 (LED shaft)

// ══════════════════════════════════════════════════════════════════════════════
// NVS (Preferences)
// ══════════════════════════════════════════════════════════════════════════════

void nvs_load() {
  prefs.begin("grctrl", true);
  homeOffsetCamera = prefs.getFloat("homeCamera", 0.0f);
  homeOffsetLed    = prefs.getFloat("homeLed",    0.0f);
  homeOffsetMotor1 = prefs.getFloat("homeM1",     0.0f);
  homeOffsetMotor2 = prefs.getFloat("homeM2",     0.0f);
  dir_flip_1       = prefs.getBool("dirFlip1",   false);
  dir_flip_2       = prefs.getBool("dirFlip2",   false);
  maxSpeedSPS      = prefs.getFloat("maxSpeed", DEFAULT_MAX_SPS);
  motorIdleMs      = prefs.getUInt("motorIdle", DEFAULT_MOTOR_IDLE_MS);
  prefs.end();
}

void nvs_save_home_camera(float v) {
  homeOffsetCamera = v;
  prefs.begin("grctrl", false);
  prefs.putFloat("homeCamera", v);
  prefs.end();
}

void nvs_save_home_led(float v) {
  homeOffsetLed = v;
  prefs.begin("grctrl", false);
  prefs.putFloat("homeLed", v);
  prefs.end();
}

void nvs_save_home_motor1(float v) {
  homeOffsetMotor1 = v;
  prefs.begin("grctrl", false);
  prefs.putFloat("homeM1", v);
  prefs.end();
}

void nvs_save_home_motor2(float v) {
  homeOffsetMotor2 = v;
  prefs.begin("grctrl", false);
  prefs.putFloat("homeM2", v);
  prefs.end();
}

void nvs_save_dir_flips() {
  prefs.begin("grctrl", false);
  prefs.putBool("dirFlip1", dir_flip_1);
  prefs.putBool("dirFlip2", dir_flip_2);
  prefs.end();
}

void nvs_save_max_speed(float v) {
  maxSpeedSPS = v;
  prefs.begin("grctrl", false);
  prefs.putFloat("maxSpeed", v);
  prefs.end();
}

void nvs_save_motor_idle(uint32_t v) {
  motorIdleMs = constrain(v, (uint32_t)MOTOR_IDLE_MS_MIN, (uint32_t)MOTOR_IDLE_MS_MAX);
  prefs.begin("grctrl", false);
  prefs.putUInt("motorIdle", motorIdleMs);
  prefs.end();
}

// ══════════════════════════════════════════════════════════════════════════════
// Software I2C (GPIO 7/15) — open-drain bit-bang, ~100 kHz
// Shared by: AS5600 #2 (0x36) and PCA9685 (0x40)
// ══════════════════════════════════════════════════════════════════════════════

// Open-drain: drive LOW by setting OUTPUT+LOW; release by INPUT_PULLUP.
inline void sw_sda_high() { pinMode(SWIRE_SDA, INPUT_PULLUP); }
inline void sw_sda_low()  { pinMode(SWIRE_SDA, OUTPUT); digitalWrite(SWIRE_SDA, LOW); }
inline void sw_scl_high() { pinMode(SWIRE_SCL, INPUT_PULLUP); }
inline void sw_scl_low()  { pinMode(SWIRE_SCL, OUTPUT); digitalWrite(SWIRE_SCL, LOW); }
inline bool sw_read_sda() { return digitalRead(SWIRE_SDA) != 0; }
inline void sw_delay()    { delayMicroseconds(5); }  // 5 µs half-bit → ~100 kHz

void sw_start() {
  sw_sda_high(); sw_scl_high(); sw_delay();
  sw_sda_low();  sw_delay();    // SDA falls while SCL is high = START
  sw_scl_low();  sw_delay();
}

void sw_stop() {
  sw_sda_low();  sw_delay();
  sw_scl_high(); sw_delay();
  sw_sda_high(); sw_delay();    // SDA rises while SCL is high = STOP
}

void sw_repeated_start() {
  sw_sda_high(); sw_delay();
  sw_scl_high(); sw_delay();
  sw_sda_low();  sw_delay();
  sw_scl_low();  sw_delay();
}

bool sw_write_byte(uint8_t data) {
  for (int i = 7; i >= 0; i--) {
    if (data & (1 << i)) sw_sda_high(); else sw_sda_low();
    sw_delay();
    sw_scl_high(); sw_delay();
    sw_scl_low();  sw_delay();
  }
  sw_sda_high();               // release SDA for ACK
  sw_scl_high(); sw_delay();
  bool ack = !sw_read_sda();   // ACK = slave pulls SDA low
  sw_scl_low();  sw_delay();
  return ack;
}

uint8_t sw_read_byte(bool send_ack) {
  uint8_t data = 0;
  sw_sda_high();               // release SDA so slave can drive
  for (int i = 7; i >= 0; i--) {
    sw_scl_high(); sw_delay();
    if (sw_read_sda()) data |= (1 << i);
    sw_scl_low();  sw_delay();
  }
  if (send_ack) sw_sda_low(); else sw_sda_high();
  sw_scl_high(); sw_delay();
  sw_scl_low();  sw_delay();
  sw_sda_high();
  return data;
}

// ── AS5600 #2 read via software I2C (LED motor shaft) ────────────────────────

int16_t readAS5600RawSW() {
  sw_start();
  if (!sw_write_byte(AS5600_ADDR << 1))         { sw_stop(); return -1; }
  if (!sw_write_byte(AS5600_RAW_HI))             { sw_stop(); return -1; }
  sw_repeated_start();
  if (!sw_write_byte((AS5600_ADDR << 1) | 0x01)) { sw_stop(); return -1; }
  uint8_t hi = sw_read_byte(true);
  uint8_t lo = sw_read_byte(false);
  sw_stop();
  return (int16_t)((hi & 0x0F) << 8) | lo;
}

float as5600DegSW() {
  int16_t raw = readAS5600RawSW();
  if (raw < 0) return 0.0f;
  return roundf(raw * 360.0f / 4096.0f * 10000.0f) / 10000.0f;
}

// LED motor shaft angle relative to home, signed (−180,180]. `reversed` follows
// the motor's effective direction (m2ShaftRev). Wraps every motor revolution.
float as5600DegSWHome(float homeOffset, bool reversed) {
  int16_t raw = readAS5600RawSW();
  if (raw < 0) return 0.0f;
  return homeRelSigned(raw * 360.0f / 4096.0f, homeOffset, reversed);
}

// ── PCA9685 minimal driver via software I2C ───────────────────────────────────

static bool pca_write_reg(uint8_t reg, uint8_t val) {
  sw_start();
  if (!sw_write_byte(PCA9685_ADDR << 1)) { sw_stop(); return false; }
  sw_write_byte(reg);
  sw_write_byte(val);
  sw_stop();
  return true;
}

void pca_begin() {
  pca_write_reg(PCA9685_MODE1, 0x00);   // clear SLEEP, wake up
  delay(1);
  // OUTDRV=1 (totem-pole) + INVRT=1 (bit4). LEDs are common-anode: anode→V+,
  // cathode→PWM output, so the channel sinks and LED-ON = output LOW. INVRT
  // makes full-off→HIGH (LED off) and brightness scale the right way.
  pca_write_reg(PCA9685_MODE2, 0x14);
  delay(1);
}

void pca_set_freq(uint16_t freq_hz) {
  // prescale = round(25 MHz / (4096 × freq)) − 1
  uint8_t prescale = (uint8_t)(25000000.0f / (4096.0f * (float)freq_hz) + 0.5f) - 1;
  pca_write_reg(PCA9685_MODE1, 0x10);   // set SLEEP (required to change prescaler)
  delay(1);
  pca_write_reg(PCA9685_PRESCALE, prescale);
  pca_write_reg(PCA9685_MODE1, 0x00);   // clear SLEEP
  delay(1);
  pca_write_reg(PCA9685_MODE1, 0xA0);   // RESTART + AI (auto-increment)
  delay(1);
}

// Write 4-byte LED register block (ON_L, ON_H, OFF_L, OFF_H) for channel ch.
// Requires auto-increment (AI bit) set in MODE1.
void pca_set_pin(uint8_t ch, uint16_t val) {
  if (ch >= 16) return;
  uint8_t reg = PCA9685_LED0_ON_L + 4 * ch;
  sw_start();
  sw_write_byte(PCA9685_ADDR << 1);
  sw_write_byte(reg);
  if (val >= 4096) {
    sw_write_byte(0x00); sw_write_byte(0x10);  // ON_L, ON_H: full-on flag (bit 4)
    sw_write_byte(0x00); sw_write_byte(0x00);  // OFF_L, OFF_H
  } else if (val == 0) {
    sw_write_byte(0x00); sw_write_byte(0x00);  // ON_L, ON_H
    sw_write_byte(0x00); sw_write_byte(0x10);  // OFF_L, OFF_H: full-off flag (bit 4)
  } else {
    sw_write_byte(0x00);                          // ON_L
    sw_write_byte(0x00);                          // ON_H
    sw_write_byte((uint8_t)(val & 0xFF));          // OFF_L
    sw_write_byte((uint8_t)((val >> 8) & 0x0F));  // OFF_H
  }
  sw_stop();
}

// ══════════════════════════════════════════════════════════════════════════════
// Hardware AS5600 helpers (Wire and Wire1)
// ══════════════════════════════════════════════════════════════════════════════

int16_t readAS5600Raw(TwoWire& bus) {
  bus.beginTransmission(AS5600_ADDR);
  bus.write(AS5600_RAW_HI);
  if (bus.endTransmission(false) != 0) return -1;
  if (bus.requestFrom((uint8_t)AS5600_ADDR, (uint8_t)2) != 2) return -1;
  uint8_t hi = bus.read();
  uint8_t lo = bus.read();
  return (int16_t)((hi & 0x0F) << 8) | lo;
}

// Angle from home position, signed (−180,180]. `reversed` negates the reading
// for output-shaft encoders that turn opposite their motor (see homeRelSigned).
float encoderDeg(TwoWire& bus, float homeOffset, bool reversed) {
  int16_t raw = readAS5600Raw(bus);
  if (raw < 0) return 0.0f;
  return homeRelSigned(raw * 360.0f / 4096.0f, homeOffset, reversed);
}

// Raw absolute angle (0–360°), no home offset.
float encoderRawDeg(TwoWire& bus) {
  int16_t raw = readAS5600Raw(bus);
  return (raw >= 0) ? roundf(raw * 360.0f / 4096.0f * 10000.0f) / 10000.0f : 0.0f;
}

// ══════════════════════════════════════════════════════════════════════════════
// OME85 absolute encoder — BiSS-C via MAX490 (RS422) + TXS0108E level shifter
// CLK = GPIO 18 (master output), DATA = GPIO 17 (slave input)
//
// The encoder returns a 17-bit frame; bit 16 (MSB) is a stuck framing bit.
// Strip it to obtain the true 16-bit position (0–65535 → 0–360°).
//
// Adapted from ArduinoCode/ESP32S-LevelShifter-RS422-Encoder.ino
// ══════════════════════════════════════════════════════════════════════════════

uint8_t biss_crc(uint32_t rawData) {
  uint8_t crc = 0;
  for (int i = BISS_DATA_BITS + 1; i >= 0; i--) {
    bool bit = (rawData >> i) & 1;
    bool inv = bit ^ ((crc >> 5) & 1);
    crc = (crc << 1) & 0x3F;
    if (inv) crc ^= BISS_CRC_POLY;
  }
  return (~crc) & 0x3F;
}

// Reads one BiSS-C frame via direct register bit-bang. Returns the raw 17-bit
// position (0..131071), or -1 on a frame error / idle pattern. CRC is read but
// NOT required: on this PCB the TXS0108E on the DATA line systematically
// corrupts the trailing EW/CRC bits while the position field stays reliable.
// (When the level shifter is fixed, re-enable the biss_crc() check here.)
int32_t ome85_read_pos() {
  uint32_t positionData = 0;
  uint8_t  errorWarning = 0;
  uint8_t  crcData      = 0;

  OME85_CLK_HIGH();
  delayMicroseconds(100);

  noInterrupts();

  // Trigger pulse
  OME85_CLK_LOW();  biss_clockDelay();
  OME85_CLK_HIGH();

  // Wait for ACK (DATA goes LOW)
  uint32_t cnt = 0;
  while (OME85_READ_DATA() == 1) {
    if (++cnt > 2000) { interrupts(); return -1; }
  }

  // Clock until start bit (DATA goes HIGH)
  cnt = 0;
  while (OME85_READ_DATA() == 0) {
    OME85_CLK_LOW();  biss_clockDelay();
    OME85_CLK_HIGH(); biss_clockDelay();
    if (++cnt > 2000) { interrupts(); return -1; }
  }

  // CDS bit — skip
  OME85_CLK_LOW();  biss_clockDelay();
  OME85_CLK_HIGH(); biss_sampleDelay();
  (void)OME85_READ_DATA();
  biss_clockDelay();

  // 17 position bits (MSB first)
  for (int i = 0; i < BISS_DATA_BITS; i++) {
    OME85_CLK_LOW();  biss_clockDelay();
    OME85_CLK_HIGH(); biss_sampleDelay();
    positionData = (positionData << 1) | OME85_READ_DATA();
    biss_clockDelay();
  }

  // 2 error/warning bits
  for (int i = 0; i < 2; i++) {
    OME85_CLK_LOW();  biss_clockDelay();
    OME85_CLK_HIGH(); biss_sampleDelay();
    errorWarning = (errorWarning << 1) | OME85_READ_DATA();
    biss_clockDelay();
  }

  // 6 CRC bits
  for (int i = 0; i < 6; i++) {
    OME85_CLK_LOW();  biss_clockDelay();
    OME85_CLK_HIGH(); biss_sampleDelay();
    crcData = (crcData << 1) | OME85_READ_DATA();
    biss_clockDelay();
  }

  // Idle pulse
  OME85_CLK_LOW();  biss_clockDelay();
  OME85_CLK_HIGH();

  interrupts();
  delayMicroseconds(100);

  (void)errorWarning; (void)crcData;   // ignored until level shifter is fixed

  if (positionData == 0 || positionData == 131071) return -1;
  return (int32_t)positionData;
}

// Majority-vote read: samples OME85_SAMPLES times and returns the raw angle
// (0–360°) of the position that appears most often (values within 2 counts fold
// together to absorb LSB dither). Returns -1.0 if no frame could be read.
// Blocking (~4 ms); call only when the camera motor is stationary.
float ome85_raw_deg() {
  int32_t vals[OME85_SAMPLES];
  int     cnt[OME85_SAMPLES];
  int     n = 0;

  for (int s = 0; s < OME85_SAMPLES; s++) {
    int32_t p = ome85_read_pos();
    if (p < 0) continue;
    int hit = -1;
    for (int i = 0; i < n; i++) {
      int32_t d = vals[i] - p; if (d < 0) d = -d;
      if (d <= 2) { hit = i; break; }
    }
    if (hit < 0) { vals[n] = p; cnt[n] = 1; n++; }
    else         { cnt[hit]++; }
  }

  if (n == 0) return -1.0f;

  int best = 0;
  for (int i = 1; i < n; i++) if (cnt[i] > cnt[best]) best = i;

  uint32_t truePos = (uint32_t)vals[best] & 0xFFFF;  // strip stuck MSB → 16-bit
  return (float)truePos / 65536.0f * 360.0f;
}

// Camera arm angle relative to stored home position, signed (−180,180]. OME85 is
// on the output shaft; sign follows the camera motor's effective direction.
float ome85DegFromHome() {
  float raw = ome85_raw_deg();
  if (raw < 0.0f) return 0.0f;
  return homeRelSigned(raw, homeOffsetCamera, camOutRev());
}

// ══════════════════════════════════════════════════════════════════════════════
// LED helpers — PCA9685 channels 0–6
// ══════════════════════════════════════════════════════════════════════════════

void setLed(uint8_t idx, uint8_t state, uint8_t brightness) {
  if (idx >= LED_COUNT) return;
  ledState[idx]      = state;
  ledBrightness[idx] = state ? brightness : 0;
  uint16_t val = state ? (uint16_t)(brightness * 4095UL / 255) : 0;
  pca_set_pin(idx, val);
}

void allLedsOff() {
  for (uint8_t i = 0; i < LED_COUNT; i++) setLed(i, 0, 0);
}

// Read one PCA9685 register over software I2C. Returns 0xFF if the chip does
// not ACK (e.g. unpowered).
uint8_t pca_read_reg(uint8_t reg) {
  sw_start();
  if (!sw_write_byte(PCA9685_ADDR << 1)) { sw_stop(); return 0xFF; }
  sw_write_byte(reg);
  sw_repeated_start();
  if (!sw_write_byte((PCA9685_ADDR << 1) | 0x01)) { sw_stop(); return 0xFF; }
  uint8_t v = sw_read_byte(false);
  sw_stop();
  return v;
}

// Ensure the PCA9685 is initialized. The chip may be powered from the external
// supply and thus come up AFTER the ESP32 has booted (or after a power cycle),
// leaving it in its default state (SLEEP=1, AI=0) where LED writes don't take.
// Detect that (AI bit clear or SLEEP set) and re-run init, then re-apply the
// current LED states. Cheap enough to poll periodically and before LED commands.
void pca_ensure() {
  uint8_t mode1 = pca_read_reg(PCA9685_MODE1);
  bool ready = (mode1 != 0xFF) && (mode1 & 0x20) && !(mode1 & 0x10); // AI=1, SLEEP=0
  if (ready) return;

  pca_begin();
  pca_set_freq(1000);
  for (uint8_t i = 0; i < LED_COUNT; i++) {
    uint16_t val = ledState[i] ? (uint16_t)(ledBrightness[i] * 4095UL / 255) : 0;
    pca_set_pin(i, val);
  }
}

// ══════════════════════════════════════════════════════════════════════════════
// Motor helpers
// ══════════════════════════════════════════════════════════════════════════════

float speedToSPS(int pct) {
  return maxSpeedSPS * constrain(pct, 1, 100) / 100.0f;
}

long cameraAngleToSteps(float outputDeg) {
  return (long)(outputDeg * CAMERA_GEAR_RATIO * STEPS_PER_REV / 360.0f);
}

long ledAngleToSteps(float outputDeg) {
  return (long)(outputDeg * LED_GEAR_RATIO * STEPS_PER_REV / 360.0f);
}

float cmdCameraDeg() {
  return motor1.currentPosition() * 360.0f / (CAMERA_GEAR_RATIO * STEPS_PER_REV);
}

float cmdLedDeg() {
  return motor2.currentPosition() * 360.0f / (LED_GEAR_RATIO * STEPS_PER_REV);
}

void enableMotors(bool en) {
  // PoStep60 ENABLE is active-LOW
  digitalWrite(M1_EN, en ? LOW : HIGH);
  digitalWrite(M2_EN, en ? LOW : HIGH);
  motorsEnabled = en;
}

// Read absolute output encoders and set step counters to match, so the firmware
// knows where the arms are without a re-home. Skips a motor if its encoder read
// fails, to avoid slamming the step counter to a false 0. Called at boot and
// whenever the drivers are re-enabled after an idle-disable (the arm may have
// drifted while de-energized).
void initStepCounters() {
  float camRaw = ome85_raw_deg();
  if (camRaw >= 0.0f) {
    float camDeg = homeRelSigned(camRaw, homeOffsetCamera, camOutRev());  // signed, dir-aware
    motor1.setCurrentPosition(cameraAngleToSteps(camDeg));
    lastCameraDeg = camDeg;
    cameraValid = true;
  }
  if (readAS5600Raw(Wire1) >= 0) {
    float ledDeg = encoderDeg(Wire1, homeOffsetLed, ledOutRev());  // LED arc output, dir-aware
    motor2.setCurrentPosition(ledAngleToSteps(ledDeg));
  }
}

// Enable the drivers before a move. If they were idle-disabled, re-sync the step
// counters to the absolute encoders first (must happen before moveTo/move, since
// setCurrentPosition also resets the target). No-op resync if already enabled.
void wakeMotors() {
  if (!motorsEnabled) {
    enableMotors(true);
    delay(2);              // let the drivers energize before reading/moving
    initStepCounters();
  }
  lastMotorActiveMs = millis();
}

// ══════════════════════════════════════════════════════════════════════════════
// JSON output helpers
// ══════════════════════════════════════════════════════════════════════════════

void sendJsonToAll(JsonDocument& doc) {
  serializeJson(doc, Serial);
  Serial.print('\n');
  if (tcpClient && tcpClient.connected()) {
    serializeJson(doc, tcpClient);
    tcpClient.print('\n');
  }
}

void sendAck(const char* cmd) {
  StaticJsonDocument<80> doc;
  doc["type"] = "ACK"; doc["cmd"] = cmd; doc["ts"] = millis();
  sendJsonToAll(doc);
}

void sendNack(const char* cmd, const char* reason) {
  StaticJsonDocument<128> doc;
  doc["type"] = "NACK"; doc["cmd"] = cmd;
  doc["reason"] = reason; doc["ts"] = millis();
  sendJsonToAll(doc);
}

void sendMoveDone(int motor, float finalAngle) {
  StaticJsonDocument<96> doc;
  doc["type"] = "MOVE_DONE"; doc["motor"] = motor;
  doc["final_angle"] = finalAngle; doc["ts"] = millis();
  sendJsonToAll(doc);
}

void sendTelemetry() {
  StaticJsonDocument<512> doc;
  doc["type"]            = "STATE";
  doc["ts"]              = millis();
  // Motor shaft encoders relative to home, signed (−180,180]; wrap every motor rev
  doc["enc_motor1_deg"]  = encoderDeg(Wire, homeOffsetMotor1, m1ShaftRev());  // camera motor shaft (AS5600 #1)
  doc["enc_motor2_deg"]  = as5600DegSWHome(homeOffsetMotor2, m2ShaftRev());   // LED motor shaft (AS5600 #2)
  // Output shaft encoders relative to home. The OME85 read is blocking (~4 ms,
  // interrupts off), so only read it when the camera motor is idle — otherwise
  // it would stall motor1.run() and roughen the motion. Reuse the cached value
  // during motion. Report JSON null when an encoder doesn't respond, so the UI
  // shows "—" instead of a misleading 0°.
  if (!motor1Moving) {
    float raw = ome85_raw_deg();
    if (raw >= 0.0f) {
      lastCameraDeg = homeRelSigned(raw, homeOffsetCamera, camOutRev());  // signed, dir-aware
      cameraValid = true;
    } else {
      cameraValid = false;
    }
  }
  if (cameraValid) doc["enc_camera_deg"] = lastCameraDeg;    // camera arm (OME85)
  else             doc["enc_camera_deg"] = nullptr;

  int16_t ledArcRaw = readAS5600Raw(Wire1);                  // LED arc rod (AS5600 #3)
  if (ledArcRaw >= 0) {
    doc["enc_led_arc_deg"] = homeRelSigned(ledArcRaw * 360.0f / 4096.0f, homeOffsetLed, ledOutRev());
  } else {
    doc["enc_led_arc_deg"] = nullptr;
  }
  // Commanded output positions derived from step counters
  doc["cmd_camera_deg"]  = cmdCameraDeg();
  doc["cmd_led_deg"]     = cmdLedDeg();
  // Motion and config status
  doc["motor1_moving"]   = motor1Moving;
  doc["motor2_moving"]   = motor2Moving;
  doc["dir_flip_1"]      = dir_flip_1;
  doc["dir_flip_2"]      = dir_flip_2;
  doc["motor_idle_ms"]   = motorIdleMs;

  JsonArray states  = doc.createNestedArray("led_states");
  JsonArray brights = doc.createNestedArray("led_brightness");
  for (uint8_t i = 0; i < LED_COUNT; i++) {
    states.add(ledState[i]);
    brights.add((uint8_t)ledBrightness[i]);
  }
  sendJsonToAll(doc);
}

// ══════════════════════════════════════════════════════════════════════════════
// Command handler
// ══════════════════════════════════════════════════════════════════════════════

void handleCommand(const char* line) {
  StaticJsonDocument<256> doc;
  if (deserializeJson(doc, line) != DeserializationError::Ok) {
    sendNack("?", "JSON parse error");
    return;
  }

  const char* type = doc["type"] | "";
  lastMsgMs = millis();

  if (strcmp(type, "PING") == 0) {
    StaticJsonDocument<64> r;
    r["type"] = "PONG"; r["ts"] = millis();
    sendJsonToAll(r);

  } else if (strcmp(type, "GET_STATE") == 0) {
    sendTelemetry();

  } else if (strcmp(type, "MOVE") == 0) {
    int   motor = doc["motor"] | 0;
    float angle = doc["angle"] | 0.0f;
    int   spd   = doc["speed"] | 50;
    float sps   = speedToSPS(spd);
    wakeMotors();   // enable + (if was idle-disabled) resync counters, before moveTo
    if (motor == 1 || motor == 0) {
      motor1.setMaxSpeed(sps);
      motor1.setAcceleration(sps * 2.0f);
      motor1.moveTo(cameraAngleToSteps(angle));
      motor1Moving = true;
    }
    if (motor == 2 || motor == 0) {
      motor2.setMaxSpeed(sps);
      motor2.setAcceleration(sps * 2.0f);
      motor2.moveTo(ledAngleToSteps(angle));
      motor2Moving = true;
    }
    sendAck("MOVE");

  } else if (strcmp(type, "JOG") == 0) {
    int   motor = doc["motor"]     | 0;
    int   dir   = doc["direction"] | 1;
    float deg   = doc["degrees"]   | 1.0f;
    int   spd   = doc["speed"]     | 30;
    float sps   = speedToSPS(spd);
    wakeMotors();   // enable + (if was idle-disabled) resync counters, before move
    if (motor == 1 || motor == 0) {
      long delta = (long)(deg * CAMERA_GEAR_RATIO * STEPS_PER_REV / 360.0f) * dir;
      motor1.setMaxSpeed(sps);
      motor1.setAcceleration(sps * 2.0f);
      motor1.move(delta);
      motor1Moving = true;
    }
    if (motor == 2 || motor == 0) {
      long delta = (long)(deg * LED_GEAR_RATIO * STEPS_PER_REV / 360.0f) * dir;
      motor2.setMaxSpeed(sps);
      motor2.setAcceleration(sps * 2.0f);
      motor2.move(delta);
      motor2Moving = true;
    }
    sendAck("JOG");

  } else if (strcmp(type, "STOP") == 0) {
    int motor = doc["motor"] | 0;
    if (motor == 1 || motor == 0) { motor1.stop(); motor1Moving = false; }
    if (motor == 2 || motor == 0) { motor2.stop(); motor2Moving = false; }
    sendAck("STOP");

  } else if (strcmp(type, "SET_HOME") == 0) {
    int motor = doc["motor"] | 0;
    if (motor == 1 || motor == 0) {
      float raw = ome85_raw_deg();
      nvs_save_home_camera(raw >= 0.0f ? raw : 0.0f);   // camera output (OME85)
      nvs_save_home_motor1(encoderRawDeg(Wire));        // camera motor shaft (AS5600 #1)
      motor1.setCurrentPosition(0);
      lastCameraDeg = 0.0f;
    }
    if (motor == 2 || motor == 0) {
      nvs_save_home_led(encoderRawDeg(Wire1));          // LED arc output (AS5600 #3)
      nvs_save_home_motor2(as5600DegSW());              // LED motor shaft (AS5600 #2)
      motor2.setCurrentPosition(0);
    }
    sendAck("SET_HOME");

  } else if (strcmp(type, "LED_SET") == 0) {
    pca_ensure();
    setLed(doc["index"] | 0, doc["state"] | 0, doc["brightness"] | 200);
    sendAck("LED_SET");

  } else if (strcmp(type, "LED_ALL") == 0) {
    pca_ensure();
    JsonArray states  = doc["states"];
    JsonArray brights = doc["brightness"];
    for (uint8_t i = 0; i < LED_COUNT && i < (uint8_t)states.size(); i++)
      setLed(i, (uint8_t)states[i], (uint8_t)brights[i]);
    sendAck("LED_ALL");

  } else if (strcmp(type, "LED_OFF_ALL") == 0) {
    pca_ensure();
    allLedsOff();
    sendAck("LED_OFF_ALL");

  } else if (strcmp(type, "SET_TELEMETRY") == 0) {
    telemetryInterval = constrain((uint16_t)(doc["interval_ms"] | 100), 50, 5000);
    sendAck("SET_TELEMETRY");

  } else if (strcmp(type, "SET_CONFIG") == 0) {
    bool changed = false;
    if (doc.containsKey("dir_flip_1")) {
      dir_flip_1 = doc["dir_flip_1"].as<bool>();
      motor1.setPinsInverted(m1Inverted(), false, false);
      changed = true;
    }
    if (doc.containsKey("dir_flip_2")) {
      dir_flip_2 = doc["dir_flip_2"].as<bool>();
      motor2.setPinsInverted(m2Inverted(), false, false);
      changed = true;
    }
    if (doc.containsKey("max_speed_sps")) {
      float v = doc["max_speed_sps"].as<float>();
      if (v > 0.0f) nvs_save_max_speed(v);
    }
    if (doc.containsKey("motor_idle_ms")) {
      nvs_save_motor_idle(doc["motor_idle_ms"].as<uint32_t>());
    }
    if (changed) nvs_save_dir_flips();
    sendAck("SET_CONFIG");
  }
}

// ══════════════════════════════════════════════════════════════════════════════
// Setup
// ══════════════════════════════════════════════════════════════════════════════

void setup() {
  Serial.begin(115200);

  // Hardware I2C 0 — AS5600 #1 (camera motor shaft)
  Wire.begin(I2C0_SDA, I2C0_SCL);
  Wire.setClock(400000);

  // Hardware I2C 1 — AS5600 #3 (LED arc rod, final output encoder)
  Wire1.begin(I2C1_SDA, I2C1_SCL);
  Wire1.setClock(400000);

  // Software I2C idle state (both lines pulled high)
  sw_sda_high();
  sw_scl_high();
  delay(5);

  // PCA9685 via software I2C — 1 kHz PWM (prescale ≈ 5 for 25 MHz oscillator)
  pca_begin();
  pca_set_freq(1000);
  allLedsOff();

  // OME85 BiSS-C GPIO
  pinMode(OME85_CLK,  OUTPUT); digitalWrite(OME85_CLK,  HIGH);
  pinMode(OME85_DATA, INPUT);

  // Encoder self-check on startup
  if (readAS5600Raw(Wire) < 0)
    Serial.println("[WARN] AS5600 #1 not responding (Wire, GPIO 10/11)");
  if (readAS5600Raw(Wire1) < 0)
    Serial.println("[WARN] AS5600 #3 not responding (Wire1, GPIO 4/5)");
  if (readAS5600RawSW() < 0)
    Serial.println("[WARN] AS5600 #2 not responding (SoftI2C, GPIO 7/15)");
  if (ome85_raw_deg() < 0.0f)
    Serial.println("[WARN] OME85 not responding (GPIO 17/18)");

  // Motor driver pins — PoStep60 EN is active-LOW; start disabled
  pinMode(M1_EN, OUTPUT); pinMode(M2_EN, OUTPUT);
  enableMotors(false);

  motor1.setMaxSpeed(DEFAULT_MAX_SPS);
  motor1.setAcceleration(DEFAULT_ACCEL_SPS2);
  motor1.setMinPulseWidth(2);  // PoStep60 requires ≥1 µs; 2 µs is safe
  motor2.setMaxSpeed(DEFAULT_MAX_SPS);
  motor2.setAcceleration(DEFAULT_ACCEL_SPS2);
  motor2.setMinPulseWidth(2);

  // Load persisted config and apply
  nvs_load();
  motor1.setPinsInverted(m1Inverted(), false, false);
  motor2.setPinsInverted(m2Inverted(), false, false);
  motor1.setMaxSpeed(maxSpeedSPS);
  motor2.setMaxSpeed(maxSpeedSPS);

  // Sync step counters to current absolute encoder positions so the firmware
  // knows where the arms are after a power cycle without requiring re-homing.
  initStepCounters();

  // WiFi — non-blocking with 10 s timeout; USB serial always available
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  Serial.print("Connecting to WiFi (10 s timeout)");
  unsigned long wifiStart = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - wifiStart < 10000) {
    delay(500); Serial.print('.');
  }
  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("\nWiFi connected. IP: %s\n", WiFi.localIP().toString().c_str());
    server.begin();
    Serial.printf("TCP server on port %d\n", TCP_PORT);
  } else {
    Serial.println("\nWiFi unavailable — USB serial only");
    WiFi.disconnect(true);
  }

  serialBuf.reserve(256);
  tcpBuf.reserve(256);
  lastMsgMs = millis();
  Serial.println("GRControl v2.0 ready");
}

// ══════════════════════════════════════════════════════════════════════════════
// Loop
// ══════════════════════════════════════════════════════════════════════════════

void loop() {
  // Stepper state machines — must execute every iteration for accurate timing
  motor1.run();
  motor2.run();

  // Detect motion completion and send MOVE_DONE with final output encoder angle
  if (motor1Moving && motor1.distanceToGo() == 0) {
    motor1Moving = false;
    float raw = ome85_raw_deg();
    if (raw >= 0.0f) {
      lastCameraDeg = homeRelSigned(raw, homeOffsetCamera, camOutRev());  // signed, dir-aware
      cameraValid = true;
    } else {
      cameraValid = false;
    }
    sendMoveDone(1, lastCameraDeg);
  }
  if (motor2Moving && motor2.distanceToGo() == 0) {
    motor2Moving = false;
    sendMoveDone(2, encoderDeg(Wire1, homeOffsetLed, ledOutRev()));  // LED arc output, dir-aware
  }

  // Auto-disable the drivers after they've been idle a while, to silence the
  // holding-current chopper buzz. Refresh the idle timer whenever a motor moves.
  if (motor1Moving || motor2Moving) {
    lastMotorActiveMs = millis();
  } else if (motorsEnabled && millis() - lastMotorActiveMs > motorIdleMs) {
    enableMotors(false);
  }

  // TCP: accept new client or detect disconnect
  if (!tcpClient || !tcpClient.connected()) {
    if (tcpClient) Serial.println("TCP client disconnected");
    tcpClient = server.available();
    if (tcpClient) { Serial.println("TCP client connected"); lastMsgMs = millis(); }
  }

  // Read TCP input line by line
  if (tcpClient && tcpClient.connected()) {
    while (tcpClient.available()) {
      char c = tcpClient.read();
      if (c == '\n') {
        if (tcpBuf.length()) handleCommand(tcpBuf.c_str());
        tcpBuf = "";
      } else {
        tcpBuf += c;
      }
    }
  }

  // Read USB Serial input line by line
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n') {
      if (serialBuf.length()) handleCommand(serialBuf.c_str());
      serialBuf = "";
    } else {
      serialBuf += c;
    }
  }

  // Watchdog: enter safe state after 10 s with no messages
  if (millis() - lastMsgMs > 10000) {
    motor1.stop(); motor1Moving = false;
    motor2.stop(); motor2Moving = false;
    allLedsOff();
    enableMotors(false);
    lastMsgMs = millis();
    Serial.println("[WATCHDOG] 10 s silence — safe state");
  }

  // Periodic PCA9685 health check (only while motors are idle — the register
  // read is a blocking soft-I2C transaction). Recovers the LED driver if it was
  // powered up after boot, and applies the current (default: all-off) states.
  if (!motor1Moving && !motor2Moving && millis() - lastPcaCheckMs >= 1000) {
    lastPcaCheckMs = millis();
    pca_ensure();
  }

  // Periodic telemetry
  if (millis() - lastTelemetryMs >= telemetryInterval) {
    lastTelemetryMs = millis();
    sendTelemetry();
  }
}
