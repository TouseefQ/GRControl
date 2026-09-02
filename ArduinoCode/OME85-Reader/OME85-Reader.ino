/*
 * OME85-Reader — minimal BiSS-C reader, no motors, no I2C.
 * Reads and prints the OME85 angle continuously so the camera gear
 * can be rotated freely by hand.
 *
 * CLK = GPIO18 (direct to RS422 TXD, no level shifter)
 * DATA = GPIO17 (through TXS0108E level shifter)
 * Serial: 115200 baud
 */

#include "soc/gpio_reg.h"

#define OME85_CLK  18
#define OME85_DATA 17

#define CLK_HIGH()   REG_WRITE(GPIO_OUT_W1TS_REG, (1UL << OME85_CLK))
#define CLK_LOW()    REG_WRITE(GPIO_OUT_W1TC_REG, (1UL << OME85_CLK))
#define READ_DATA()  ((REG_READ(GPIO_IN_REG) >> OME85_DATA) & 1UL)

#define DATA_BITS  17
#define CRC_BITS    6
#define CRC_POLY 0x43

// Exact timing from the working standalone (ESP32-S3-with-OME85.ino): 40/30 NOPs.
inline void clockDelay()  { for (int n = 0; n < 40; n++) __asm__("nop;"); }
inline void sampleDelay() { for (int n = 0; n < 30; n++) __asm__("nop;"); }

static uint8_t biss_crc(uint32_t raw) {
  uint8_t crc = 0;
  for (int i = DATA_BITS + 1; i >= 0; i--) {
    bool b = (raw >> i) & 1, inv = b ^ ((crc >> 5) & 1);
    crc = (crc << 1) & 0x3F;
    if (inv) crc ^= CRC_POLY;
  }
  return (~crc) & 0x3F;
}

// Reads one frame. Returns the raw 17-bit position (0..0x1FFFF), or -1 on a
// frame error / idle pattern. Sets *crcOk to whether the CRC validated — but
// callers using majority vote ignore it, since the position field is reliable
// even when the TXS0108E corrupts the trailing EW/CRC bits.
static int32_t readOME85_raw(bool *crcOk) {
  uint32_t pos = 0;
  uint8_t  ew = 0, crc = 0;
  if (crcOk) *crcOk = false;

  CLK_HIGH();
  delayMicroseconds(100);

  noInterrupts();

  CLK_LOW();  clockDelay();
  CLK_HIGH();

  uint32_t cnt = 0;
  while (READ_DATA() == 1) {
    if (++cnt > 2000) { interrupts(); return -1; }
  }

  cnt = 0;
  while (READ_DATA() == 0) {
    CLK_LOW();  clockDelay();
    CLK_HIGH(); clockDelay();
    if (++cnt > 2000) { interrupts(); return -1; }
  }

  CLK_LOW();  clockDelay();
  CLK_HIGH(); sampleDelay();
  (void)READ_DATA();               // CDS bit — skip
  clockDelay();

  for (int i = 0; i < DATA_BITS; i++) {
    CLK_LOW();  clockDelay();
    CLK_HIGH(); sampleDelay();
    pos = (pos << 1) | READ_DATA();
    clockDelay();
  }

  for (int i = 0; i < 2; i++) {
    CLK_LOW();  clockDelay();
    CLK_HIGH(); sampleDelay();
    ew = (ew << 1) | READ_DATA();
    clockDelay();
  }

  for (int i = 0; i < CRC_BITS; i++) {
    CLK_LOW();  clockDelay();
    CLK_HIGH(); sampleDelay();
    crc = (crc << 1) | READ_DATA();
    clockDelay();
  }

  CLK_LOW();  clockDelay();
  CLK_HIGH();

  interrupts();
  delayMicroseconds(100);

  // Reject the two idle patterns (stuck LOW / stuck HIGH = aborted frame)
  if (pos == 0 || pos == 0x1FFFF) return -1;

  if (crcOk) *crcOk = (biss_crc((pos << 2) | ew) == crc);
  return (int32_t)pos;
}

// Reads the encoder `samples` times and returns the position that appears most
// often (the mode). Corrupt EW/CRC bits don't affect the position field, so the
// true position dominates the tally. Returns angle in degrees, or -1 if no
// frame could be read at all.
static float readOME85_vote(int samples, int *modeCount, int *crcPasses) {
  int32_t vals[32];
  int     cnt[32];
  int     n = 0, crcOk = 0;
  if (samples > 32) samples = 32;

  for (int s = 0; s < samples; s++) {
    bool ok = false;
    int32_t p = readOME85_raw(&ok);
    if (p < 0) continue;
    if (ok) crcOk++;
    // tally: exact match, or within 2 counts (LSB dither) folds into one bucket
    int hit = -1;
    for (int i = 0; i < n; i++) {
      int32_t d = vals[i] - p; if (d < 0) d = -d;
      if (d <= 2) { hit = i; break; }
    }
    if (hit < 0 && n < 32) { vals[n] = p; cnt[n] = 1; n++; }
    else if (hit >= 0)     { cnt[hit]++; }
  }
  if (crcPasses) *crcPasses = crcOk;

  if (n == 0) { if (modeCount) *modeCount = 0; return -1.0f; }

  int best = 0;
  for (int i = 1; i < n; i++) if (cnt[i] > cnt[best]) best = i;
  if (modeCount) *modeCount = cnt[best];

  return (uint16_t)(vals[best] & 0xFFFF) * 360.0f / 65536.0f;
}

void setup() {
  Serial.begin(115200);
  unsigned long t0 = millis();
  while (!Serial && millis() - t0 < 5000) delay(10);

  pinMode(OME85_CLK,  OUTPUT); CLK_HIGH();
  pinMode(OME85_DATA, INPUT);
  delay(100);

  Serial.println("--- OME85 reader (no motors) ---");
  Serial.println("Rotate the camera gear by hand and watch the angle change.");
  Serial.println();
}

void loop() {
  int modeCount = 0, crcPasses = 0;
  const int SAMPLES = 15;
  float a = readOME85_vote(SAMPLES, &modeCount, &crcPasses);

  if (a >= 0.0f)
    Serial.printf("%7.2f deg   (agreed %d/%d, crc-ok %d/%d)\n",
                  a, modeCount, SAMPLES, crcPasses, SAMPLES);
  else
    Serial.printf("  no frame at all (%d samples)\n", SAMPLES);

  delay(200);
}
