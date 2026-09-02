/*
 * PCA9685-Test.ino — isolated LED / PCA9685 bring-up (no motors, no encoders)
 *
 * Bus: software I2C, SDA=7 SCL=15 (same pins as GRControl-Full).
 * PCA9685 at 0x40, LED channels 0..6.
 *
 * Purpose: confirm I2C comms and find the correct output polarity for your
 * LED wiring (common-anode vs common-cathode).
 *
 * Serial menu (115200 baud), send a single character:
 *   p : probe/scan the software-I2C bus (does 0x40 ACK?)
 *   a : all LEDs full ON
 *   o : all LEDs full OFF
 *   d : all LEDs ~50% brightness
 *   0..6 : toggle that channel full on/off
 *   i : toggle INVRT (output inversion) and re-init  <-- the key test
 */

#include <Arduino.h>

#define SWIRE_SDA  7
#define SWIRE_SCL 15

#define PCA9685_ADDR      0x40
#define PCA9685_MODE1     0x00
#define PCA9685_MODE2     0x01
#define PCA9685_PRESCALE  0xFE
#define PCA9685_LED0_ON_L 0x06
#define LED_COUNT         7

bool    invert = true;               // common-anode LEDs (anode→V+, cathode→PWM); press 'i' to flip
uint8_t chanOn[LED_COUNT] = {};      // track per-channel state for toggling

// ── Software I2C (open-drain, internal pull-ups, ~100 kHz) ────────────────────
inline void sda_hi() { pinMode(SWIRE_SDA, INPUT_PULLUP); }
inline void sda_lo() { pinMode(SWIRE_SDA, OUTPUT); digitalWrite(SWIRE_SDA, LOW); }
inline void scl_hi() { pinMode(SWIRE_SCL, INPUT_PULLUP); }
inline void scl_lo() { pinMode(SWIRE_SCL, OUTPUT); digitalWrite(SWIRE_SCL, LOW); }
inline bool rd_sda() { return digitalRead(SWIRE_SDA) != 0; }
inline void dly()    { delayMicroseconds(5); }

void i2c_start() { sda_hi(); scl_hi(); dly(); sda_lo(); dly(); scl_lo(); dly(); }
void i2c_stop()  { sda_lo(); dly(); scl_hi(); dly(); sda_hi(); dly(); }

bool i2c_write(uint8_t b) {
  for (int i = 7; i >= 0; i--) {
    if (b & (1 << i)) sda_hi(); else sda_lo();
    dly(); scl_hi(); dly(); scl_lo(); dly();
  }
  sda_hi(); scl_hi(); dly();
  bool ack = !rd_sda();
  scl_lo(); dly();
  return ack;
}

bool pca_write(uint8_t reg, uint8_t val) {
  i2c_start();
  if (!i2c_write(PCA9685_ADDR << 1)) { i2c_stop(); return false; }
  i2c_write(reg);
  i2c_write(val);
  i2c_stop();
  return true;
}

// Probe every 7-bit address; report which ACK (should see 0x40).
void i2c_scan() {
  Serial.println("Scanning software-I2C bus...");
  int found = 0;
  for (uint8_t a = 1; a < 127; a++) {
    i2c_start();
    bool ack = i2c_write(a << 1);
    i2c_stop();
    if (ack) { Serial.printf("  ACK at 0x%02X\n", a); found++; }
    delay(2);
  }
  Serial.printf("Scan done, %d device(s) found.\n", found);
}

void pca_begin() {
  bool ok = pca_write(PCA9685_MODE1, 0x00);         // wake (clear SLEEP)
  delay(1);
  // MODE2: OUTDRV=1 (totem-pole). INVRT (bit4) set when invert==true.
  pca_write(PCA9685_MODE2, invert ? 0x14 : 0x04);
  delay(1);
  // set ~1 kHz: prescale = round(25e6 / (4096*1000)) - 1 = 5
  pca_write(PCA9685_MODE1, 0x10);                   // SLEEP to set prescale
  delay(1);
  pca_write(PCA9685_PRESCALE, 5);
  pca_write(PCA9685_MODE1, 0x00);                   // wake
  delay(1);
  pca_write(PCA9685_MODE1, 0xA0);                   // RESTART + auto-increment
  delay(1);
  Serial.printf("pca_begin: MODE2=%s, comms=%s\n",
                invert ? "0x14 (INVRT)" : "0x04 (normal)", ok ? "OK" : "NO ACK");
}

void pca_set_pin(uint8_t ch, uint16_t val) {
  if (ch >= 16) return;
  uint8_t reg = PCA9685_LED0_ON_L + 4 * ch;
  i2c_start();
  i2c_write(PCA9685_ADDR << 1);
  i2c_write(reg);
  if (val >= 4096) {                 // full ON
    i2c_write(0x00); i2c_write(0x10);
    i2c_write(0x00); i2c_write(0x00);
  } else if (val == 0) {             // full OFF
    i2c_write(0x00); i2c_write(0x00);
    i2c_write(0x00); i2c_write(0x10);
  } else {                            // PWM
    i2c_write(0x00); i2c_write(0x00);
    i2c_write((uint8_t)(val & 0xFF));
    i2c_write((uint8_t)((val >> 8) & 0x0F));
  }
  i2c_stop();
}

void setAll(uint16_t val) {
  for (uint8_t i = 0; i < LED_COUNT; i++) {
    pca_set_pin(i, val);
    chanOn[i] = (val > 0);
  }
}

void printMenu() {
  Serial.println("\n--- PCA9685 LED tester ---");
  Serial.println("p=scan  a=all on  o=all off  d=all 50%  0..6=toggle chan  i=toggle INVRT");
}

void setup() {
  Serial.begin(115200);
  unsigned long t0 = millis();
  while (!Serial && millis() - t0 < 3000) delay(10);

  sda_hi(); scl_hi();
  delay(5);

  i2c_scan();
  pca_begin();
  setAll(0);                 // attempt all-off
  Serial.println("All channels commanded OFF.");
  printMenu();
}

void loop() {
  if (!Serial.available()) return;
  char c = Serial.read();
  if (c == '\n' || c == '\r') return;

  if (c == 'p') { i2c_scan(); }
  else if (c == 'a') { setAll(4096); Serial.println("ALL ON"); }
  else if (c == 'o') { setAll(0);    Serial.println("ALL OFF"); }
  else if (c == 'd') { setAll(2048); Serial.println("ALL 50%"); }
  else if (c == 'i') { invert = !invert; pca_begin(); setAll(0);
                       Serial.printf("INVRT now %s; all commanded OFF\n", invert ? "ON" : "OFF"); }
  else if (c >= '0' && c <= '6') {
    uint8_t ch = c - '0';
    chanOn[ch] = !chanOn[ch];
    pca_set_pin(ch, chanOn[ch] ? 4096 : 0);
    Serial.printf("chan %d -> %s\n", ch, chanOn[ch] ? "ON" : "OFF");
  }
}
