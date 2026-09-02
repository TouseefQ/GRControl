#include <Arduino.h>

const int CLOCK_PIN = 26; 
const int DATA_PIN = 27;  

const int DATA_BITS = 17; 
const int CRC_BITS = 6;
const uint8_t CRC_POLY = 0x43; 

void setup() {
  Serial.begin(115200);
  pinMode(CLOCK_PIN, OUTPUT);
  pinMode(DATA_PIN, INPUT);
  digitalWrite(CLOCK_PIN, HIGH);
  delay(10);
  Serial.println("BiSS-C Reader Starting... (Rolled back to stable version)");
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

// 15 NOPs: Fast enough to stay above 0.5 MHz, slow enough for the level shifter
inline void clockDelay() {
  for (int nop = 0; nop < 15; nop++) __asm__("nop;");
}

void loop() {
  uint32_t positionData = 0;
  uint8_t errorWarning = 0;
  uint8_t crcData = 0;

  digitalWrite(CLOCK_PIN, LOW);
  clockDelay();
  digitalWrite(CLOCK_PIN, HIGH);
  
  uint32_t timeout = micros();
  while(digitalRead(DATA_PIN) == HIGH) {
    if (micros() - timeout > 1000) return; 
  }

  uint32_t startTimeout = micros();
  while(digitalRead(DATA_PIN) == LOW) {
    digitalWrite(CLOCK_PIN, LOW);
    clockDelay();
    digitalWrite(CLOCK_PIN, HIGH);
    clockDelay();
    if (micros() - startTimeout > 5000) return;
  }

  // == CRITICAL TIMING SECTION ==
  noInterrupts(); 

  // Skip CDS bit
  digitalWrite(CLOCK_PIN, LOW); 
  clockDelay();
  digitalWrite(CLOCK_PIN, HIGH); 
  clockDelay();

  // Read Position Data
  for (int i = 0; i < DATA_BITS; i++) {
    digitalWrite(CLOCK_PIN, LOW); 
    clockDelay();                     
    digitalWrite(CLOCK_PIN, HIGH); 
    clockDelay();                     
    positionData = (positionData << 1) | digitalRead(DATA_PIN);
  }

  // Read EW Bits
  for (int i = 0; i < 2; i++) {
    digitalWrite(CLOCK_PIN, LOW); 
    clockDelay();
    digitalWrite(CLOCK_PIN, HIGH); 
    clockDelay();
    errorWarning = (errorWarning << 1) | digitalRead(DATA_PIN);
  }

  // Read CRC Bits
  for (int i = 0; i < CRC_BITS; i++) {
    digitalWrite(CLOCK_PIN, LOW); 
    clockDelay();
    digitalWrite(CLOCK_PIN, HIGH); 
    clockDelay();
    crcData = (crcData << 1) | digitalRead(DATA_PIN);
  }

  interrupts(); 
  // == END CRITICAL TIMING ==

  // 7. Timeout (Return to idle)
  digitalWrite(CLOCK_PIN, LOW); 
  clockDelay();
  digitalWrite(CLOCK_PIN, HIGH);
  delayMicroseconds(100); 

  // 8. Process Data
  uint32_t dataForCRC = (positionData << 2) | errorWarning;
  uint8_t expectedCRC = calculateCRC(dataForCRC);
  
  // THE GREEN-ONLY FILTER
  if (expectedCRC == crcData && positionData != 0 && positionData != 131071) {
    
    // Chop off the stuck bit to reveal the true 16-bit position (0 to 65535)
    uint32_t truePosition = positionData & 0xFFFF;
    
    Serial.print("GREEN ZONE HIT! Position: ");
    Serial.print(truePosition);
    
    // Calculate the angle using the true 16-bit maximum (65536)
    float angle = ((float)truePosition / 65536.0) * 360.0;
    
    Serial.print(" | Angle: ");
    Serial.print(angle, 2);
    Serial.println(" deg");
  } 

  delay(20); 
}