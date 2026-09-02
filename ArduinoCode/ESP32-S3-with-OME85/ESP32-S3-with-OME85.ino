#include <Arduino.h>
#include "soc/gpio_reg.h"

const int CLOCK_PIN = 18; // GPIO 18 -> Connected directly to RS422 TXD
const int DATA_PIN = 17;  // GPIO 17 -> Connected to Level Shifter A8

const int DATA_BITS = 17; 
const int CRC_BITS = 6;
const uint8_t CRC_POLY = 0x43; 

// Hardware Register Macros for ESP32-S3 (Pins 0–31)
#define CLK_HIGH()  REG_WRITE(GPIO_OUT_W1TS_REG, (1UL << CLOCK_PIN))
#define CLK_LOW()   REG_WRITE(GPIO_OUT_W1TC_REG, (1UL << CLOCK_PIN))
#define READ_DATA() ((REG_READ(GPIO_IN_REG) >> DATA_PIN) & 1UL)

void setup() {
  Serial.begin(115200);
  
  uint32_t startTime = millis();
  while (!Serial && (millis() - startTime < 3000));
  
  pinMode(CLOCK_PIN, OUTPUT);
  pinMode(DATA_PIN, INPUT);
  CLK_HIGH();
  delay(100);
  Serial.println("--- ESP32-S3 BiSS-C Reader (High-Speed Motion Tracking) ---");
}

uint8_t calculateCRC(uint32_t rawData) {
  uint8_t crc = 0;
  for (int i = DATA_BITS + 1; i >= 0; i--) {
    bool bit = (rawData >> i) & 1;
    bool inv = bit ^ ((crc >> 5) & 1);
    crc = (crc << 1) & 0x3F; 
    if (inv) {
      crc ^= CRC_POLY;
    }
  }
  return (~crc) & 0x3F; 
}

// Micro-delays tuned for ~500kHz clock with level shifter stabilization
inline void clockPhaseDelay() {
  for (int nop = 0; nop < 40; nop++) __asm__("nop;");
}

inline void sampleSettleDelay() {
  for (int nop = 0; nop < 30; nop++) __asm__("nop;");
}

inline uint8_t readBit() {
  CLK_LOW();
  clockPhaseDelay();
  CLK_HIGH();
  sampleSettleDelay(); 
  uint8_t val = READ_DATA();
  clockPhaseDelay();
  return val;
}

void loop() {
  uint32_t positionData = 0;
  uint8_t errorWarning = 0;
  uint8_t crcData = 0;
  static uint32_t lastPosition = 999999;

  // == COMPLETE ATOMIC TRANSACTION ==
  // Disabling interrupts before the handshake prevents BiSS frame timeouts
  noInterrupts(); 

  CLK_LOW();
  clockPhaseDelay();
  CLK_HIGH();
  
  // Wait for ACK bit (SLO goes LOW)
  uint32_t timeoutCount = 0;
  while(READ_DATA() == 1) {
    if (++timeoutCount > 2000) {
      interrupts();
      return; 
    }
  }

  // Wait for Start bit (SLO goes HIGH)
  timeoutCount = 0;
  while(READ_DATA() == 0) {
    CLK_LOW();
    clockPhaseDelay();
    CLK_HIGH();
    clockPhaseDelay();
    if (++timeoutCount > 2000) {
      interrupts();
      return;
    }
  }

  // Skip CDS bit
  readBit();

  // Read Position Data (17 Bits)
  for (int i = 0; i < DATA_BITS; i++) {
    positionData = (positionData << 1) | readBit();
  }

  // Read Error & Warning Bits (2 Bits)
  for (int i = 0; i < 2; i++) {
    errorWarning = (errorWarning << 1) | readBit();
  }

  // Read CRC Bits (6 Bits)
  for (int i = 0; i < CRC_BITS; i++) {
    crcData = (crcData << 1) | readBit();
  }

  // Return MA line to idle state
  CLK_LOW(); 
  clockPhaseDelay();
  CLK_HIGH();

  interrupts(); 
  // == END ATOMIC TRANSACTION ==

  // Timeout recovery delay for encoder logic
  delayMicroseconds(50); 

  // CRC and Position Processing
  uint32_t dataForCRC = (positionData << 2) | errorWarning;
  uint8_t expectedCRC = calculateCRC(dataForCRC);
  
  if (positionData != 0 && positionData != 131071) {
    if (expectedCRC == crcData && errorWarning == 3) {
      uint32_t truePosition = positionData & 0xFFFF;
      
      // Print on position change (> 2 counts / ~0.01 deg)
      if (abs((int32_t)truePosition - (int32_t)lastPosition) > 2) {
        float angle = ((float)truePosition / 65536.0) * 360.0;
        
        Serial.print("Position: ");
        Serial.print(truePosition);
        Serial.print(" | Angle: ");
        Serial.print(angle, 2);
        Serial.println(" deg");
        
        lastPosition = truePosition;
      }
    }
  }

  delay(2); // 500 Hz sample rate for continuous motion capture
}