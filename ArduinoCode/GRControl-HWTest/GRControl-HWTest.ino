/*
 * GRControl Hardware Test v3
 *
 * Sequence (repeats forever):
 *   1. Print encoder readings  (before any movement)
 *   2. Both motors do 1 full motor-shaft rotation
 *   3. Stop for 3 s
 *   4. Print encoder readings
 *   5. Repeat steps 2-4 for ROTS_PER_DIRECTION rotations
 *   6. Reverse direction, repeat from step 2
 *
 * ROTS_PER_DIRECTION = 8 (= camera gear ratio 8:1)
 *   → camera arm output shaft travels exactly 360° per half-cycle
 *   → LED arc output shaft travels 8 × (360°/3) = 960° per half-cycle
 *     (LED gear ratio is 3:1; adjust ROTS_PER_DIRECTION to 3 if needed)
 *
 * Encoders:
 *   AS5600 #1 — Motor 1 shaft (camera motor)  Wire  SDA=10 SCL=11
 *   AS5600 #2 — Motor 2 shaft (LED motor)      SoftI2C SDA=7 SCL=15
 *   OME85     — Camera arm output shaft        BiSS-C CLK=18 DATA=17
 *   AS5600 #3 (LED arc rod) — not connected, skipped.
 *
 * Serial: 115200 baud. Open monitor BEFORE powering the board.
 */

#include <AccelStepper.h>
#include <Wire.h>
#include "soc/gpio_reg.h"

// ── Pins ─────────────────────────────────────────────────────────────────────
#define M1_STEP  21    // Camera arm motor
#define M1_DIR   47
#define M1_EN    48    // PoStep60 EN — active LOW

#define M2_STEP  38    // LED arc motor
#define M2_DIR    2
#define M2_EN     1    // PoStep60 EN — active LOW

#define I2C0_SDA  10
#define I2C0_SCL  11

#define SWIRE_SDA  7
#define SWIRE_SCL 15

#define OME85_CLK  18
#define OME85_DATA 17

// Direct register access — bypasses GPIO matrix overhead that causes ACK misses
#define OME85_CLK_HIGH()   REG_WRITE(GPIO_OUT_W1TS_REG, (1UL << OME85_CLK))
#define OME85_CLK_LOW()    REG_WRITE(GPIO_OUT_W1TC_REG, (1UL << OME85_CLK))
#define OME85_READ_DATA()  ((REG_READ(GPIO_IN_REG) >> OME85_DATA) & 1UL)

// clockDelay: CLK LOW/trailing phase (~667 ns at 240 MHz, matches standalone)
// sampleDelay: settle after CLK_HIGH before sampling — 30 NOPs = ~545 kHz total,
//   above the OME85's 500 kHz minimum and identical to the working standalone sketch
inline void biss_clockDelay()  { for (int n = 0; n < 40; n++) __asm__("nop;"); }
inline void biss_sampleDelay() { for (int n = 0; n < 30;  n++) __asm__("nop;"); }

// ── Motion parameters ─────────────────────────────────────────────────────────
#define STEPS_PER_REV    3200      // 1/16 microstepping = 1 motor shaft revolution
#define MAX_SPEED        3200.0f   // steps/sec  (1 motor rev/sec)
#define ACCEL            6400.0f   // steps/sec²
#define ROTS_PER_DIRECTION  8      // rotations before reversing
                                   // 8 = camera 360° output (8:1 gear)
                                   // 3 = LED arc 360° output (3:1 gear)
#define PAUSE_MS         3000      // pause between rotations (ms)

// ── Encoders ──────────────────────────────────────────────────────────────────
#define AS5600_ADDR 0x36

static float as5600_hw(TwoWire &bus) {
  bus.beginTransmission(AS5600_ADDR);
  bus.write(0x0C);
  if (bus.endTransmission(false) != 0) return -1.0f;
  bus.requestFrom((uint8_t)AS5600_ADDR, (uint8_t)2);
  if (bus.available() < 2) return -1.0f;
  uint16_t raw = ((uint16_t)(bus.read() & 0x0F) << 8) | bus.read();
  return raw * 360.0f / 4096.0f;
}

static inline void sda_hi() { pinMode(SWIRE_SDA, INPUT_PULLUP); }
static inline void sda_lo() { pinMode(SWIRE_SDA, OUTPUT); digitalWrite(SWIRE_SDA, LOW); }
static inline void scl_hi() { pinMode(SWIRE_SCL, INPUT_PULLUP); }
static inline void scl_lo() { pinMode(SWIRE_SCL, OUTPUT); digitalWrite(SWIRE_SCL, LOW); }
static inline int  sda_rd() { pinMode(SWIRE_SDA, INPUT_PULLUP); return digitalRead(SWIRE_SDA); }

static void sw_start() { sda_hi(); scl_hi(); delayMicroseconds(5); sda_lo(); delayMicroseconds(5); scl_lo(); delayMicroseconds(5); }
static void sw_stop()  { sda_lo(); delayMicroseconds(5); scl_hi(); delayMicroseconds(5); sda_hi(); delayMicroseconds(5); }

static bool sw_write(uint8_t b) {
  for (int i = 7; i >= 0; i--) {
    if (b & (1 << i)) sda_hi(); else sda_lo();
    delayMicroseconds(5); scl_hi(); delayMicroseconds(5); scl_lo(); delayMicroseconds(5);
  }
  sda_hi(); delayMicroseconds(5); scl_hi(); delayMicroseconds(5);
  bool ack = (sda_rd() == LOW); scl_lo(); delayMicroseconds(5);
  return ack;
}

static uint8_t sw_read(bool sendAck) {
  uint8_t b = 0; sda_hi();
  for (int i = 7; i >= 0; i--) {
    scl_hi(); delayMicroseconds(5); if (sda_rd()) b |= (1 << i); scl_lo(); delayMicroseconds(5);
  }
  if (sendAck) sda_lo(); else sda_hi();
  delayMicroseconds(5); scl_hi(); delayMicroseconds(5); scl_lo(); delayMicroseconds(5); sda_hi();
  return b;
}

static float as5600_sw() {
  sw_start();
  if (!sw_write(AS5600_ADDR << 1)) { sw_stop(); return -1.0f; }
  sw_write(0x0C); sw_start(); sw_write((AS5600_ADDR << 1) | 1);
  uint8_t hi = sw_read(true), lo = sw_read(false); sw_stop();
  uint16_t raw = ((uint16_t)(hi & 0x0F) << 8) | lo;
  return raw * 360.0f / 4096.0f;
}

#define DATA_BITS       17
#define CRC_BITS         6
#define CRC_POLY      0x43

// Return codes: >=0 angle, -1 ACK timeout, -2 CRC error, -3 start-bit timeout
static uint8_t biss_crc(uint32_t raw) {
  uint8_t crc = 0;
  for (int i = DATA_BITS + 1; i >= 0; i--) {
    bool b = (raw >> i) & 1, inv = b ^ ((crc >> 5) & 1);
    crc = (crc << 1) & 0x3F; if (inv) crc ^= CRC_POLY;
  }
  return (~crc) & 0x3F;
}

// Reads one BiSS-C frame. Returns raw 17-bit position (0..0x1FFFF), or -1 on a
// frame error / idle pattern. CRC is read but NOT required — on this PCB the
// TXS0108E on the DATA line systematically corrupts the trailing EW/CRC bits,
// while the position field stays reliable. We recover the position by majority
// vote across several reads (see ome85()).
static int32_t ome85_raw() {
  uint32_t pos = 0;
  uint8_t  ew = 0, crc = 0;

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

  // 17 position bits
  for (int i = 0; i < DATA_BITS; i++) {
    OME85_CLK_LOW();  biss_clockDelay();
    OME85_CLK_HIGH(); biss_sampleDelay();
    pos = (pos << 1) | OME85_READ_DATA();
    biss_clockDelay();
  }

  // 2 EW bits
  for (int i = 0; i < 2; i++) {
    OME85_CLK_LOW();  biss_clockDelay();
    OME85_CLK_HIGH(); biss_sampleDelay();
    ew = (ew << 1) | OME85_READ_DATA();
    biss_clockDelay();
  }

  // 6 CRC bits
  for (int i = 0; i < CRC_BITS; i++) {
    OME85_CLK_LOW();  biss_clockDelay();
    OME85_CLK_HIGH(); biss_sampleDelay();
    crc = (crc << 1) | OME85_READ_DATA();
    biss_clockDelay();
  }

  // Idle pulse
  OME85_CLK_LOW();  biss_clockDelay();
  OME85_CLK_HIGH();

  interrupts();
  delayMicroseconds(100);

  (void)crc;   // CRC intentionally ignored; kept for future hardware fix

  // Reject the two idle patterns (stuck LOW / stuck HIGH = aborted frame)
  if (pos == 0 || pos == 0x1FFFF) return -1;
  return (int32_t)pos;
}

// Majority-vote read: samples the encoder OME85_SAMPLES times and returns the
// position that appears most often (positions within 2 counts fold together to
// absorb LSB dither). Returns angle in degrees, or -1 if no frame was read.
#define OME85_SAMPLES 15
static float ome85() {
  int32_t vals[OME85_SAMPLES];
  int     cnt[OME85_SAMPLES];
  int     n = 0;

  for (int s = 0; s < OME85_SAMPLES; s++) {
    int32_t p = ome85_raw();
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
  return (uint16_t)(vals[best] & 0xFFFF) * 360.0f / 65536.0f;
}

// ── Helpers ───────────────────────────────────────────────────────────────────
static void printEnc(const char *label) {
  float e1 = as5600_hw(Wire);
  float e2 = as5600_sw();

  // Briefly disable both drivers to kill chopper noise during the BiSS-C read.
  digitalWrite(M1_EN, HIGH);
  digitalWrite(M2_EN, HIGH);
  delay(20);
  float e3 = ome85();
  digitalWrite(M1_EN, LOW);
  digitalWrite(M2_EN, LOW);
  Serial.printf("%-22s  M1(AS5600#1): ", label);
  if (e1 < 0) Serial.print("  ERR  "); else Serial.printf("%7.2f°", e1);
  Serial.print("   M2(AS5600#2): ");
  if (e2 < 0) Serial.print("  ERR  "); else Serial.printf("%7.2f°", e2);
  Serial.print("   Cam-out(OME85): ");
  if (e3 < 0.0f) Serial.println("NO FRAME");
  else           Serial.printf("%7.2f°\n", e3);
  Serial.flush();
}

// ── Motors ────────────────────────────────────────────────────────────────────
AccelStepper motor1(AccelStepper::DRIVER, M1_STEP, M1_DIR);
AccelStepper motor2(AccelStepper::DRIVER, M2_STEP, M2_DIR);

// ── State machine ─────────────────────────────────────────────────────────────
enum State { MOVING, PAUSING };
State state = MOVING;
unsigned long pauseStart = 0;
int  rotsDone    = 0;
bool goingFwd    = true;

static void startNextRotation() {
  long delta = goingFwd ? STEPS_PER_REV : -(long)STEPS_PER_REV;
  motor1.moveTo(motor1.currentPosition() + delta);
  motor2.moveTo(motor2.currentPosition() + delta);
  state = MOVING;
  Serial.printf("\n[%s  step %d/%d]  Moving...\n",
                goingFwd ? "FWD ▶" : "BWD ◀", rotsDone + 1, ROTS_PER_DIRECTION);
  Serial.flush();
}

// ── Setup ─────────────────────────────────────────────────────────────────────
void setup() {
  Serial.begin(115200);
  unsigned long t0 = millis();
  while (!Serial && millis() - t0 < 5000) delay(10);
  delay(300);

  Serial.println("\n============================================");
  Serial.println("  GRControl Hardware Test  v3");
  Serial.printf ("  %d rotations per direction  |  %d ms pause\n",
                 ROTS_PER_DIRECTION, PAUSE_MS);
  Serial.println("  Both motors running  |  OME85 reads camera output shaft");
  Serial.println("============================================\n");
  Serial.flush();

  // Both motors enabled (EN active LOW)
  pinMode(M1_EN,   OUTPUT); digitalWrite(M1_EN,   LOW);
  pinMode(M2_EN,   OUTPUT); digitalWrite(M2_EN,   LOW);
  pinMode(M1_DIR,  OUTPUT); pinMode(M2_DIR,  OUTPUT);
  pinMode(M1_STEP, OUTPUT); pinMode(M2_STEP, OUTPUT);
  delay(100);

  // Peripherals
  Wire.begin(I2C0_SDA, I2C0_SCL);
  Wire.setClock(400000);
  pinMode(OME85_CLK,  OUTPUT);     OME85_CLK_HIGH();
  pinMode(OME85_DATA, INPUT);
  sda_hi(); scl_hi();

  // AccelStepper
  motor1.setMaxSpeed(MAX_SPEED);
  motor1.setAcceleration(ACCEL);
  motor2.setMaxSpeed(MAX_SPEED);
  motor2.setAcceleration(ACCEL);

  // Print initial encoder readings before any movement
  printEnc("[INITIAL]");
  Serial.println();

  // Start first rotation
  startNextRotation();
}

// ── Loop ──────────────────────────────────────────────────────────────────────
void loop() {
  motor1.run();
  motor2.run();

  switch (state) {

    case MOVING:
      if (motor1.distanceToGo() == 0 && motor2.distanceToGo() == 0) {
        state = PAUSING;
        pauseStart = millis();
        Serial.println("  Stopped. Pausing 3 s...");
        Serial.flush();
      }
      break;

    case PAUSING:
      if (millis() - pauseStart >= PAUSE_MS) {
        // Print encoders after this rotation
        char label[32];
        snprintf(label, sizeof(label), "[%s rot %d/%d]",
                 goingFwd ? "FWD" : "BWD", rotsDone + 1, ROTS_PER_DIRECTION);
        printEnc(label);

        rotsDone++;
        if (rotsDone >= ROTS_PER_DIRECTION) {
          rotsDone = 0;
          goingFwd = !goingFwd;
          Serial.printf("\n======= Direction reversed → now %s =======\n",
                        goingFwd ? "FWD ▶" : "BWD ◀");
          Serial.flush();
        }
        startNextRotation();
      }
      break;
  }
}
