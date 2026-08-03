#include <Arduino.h>
#include <WiFi.h>
#include <Wire.h>
#include <Adafruit_PWMServoDriver.h>
#include <ArduinoJson.h>

// ── Config ─────────────────────────────────────────────────────────────────
const char* WIFI_SSID = "YOUR_SSID";
const char* WIFI_PASS = "YOUR_PASSWORD";
const uint16_t TCP_PORT = 8888;

// PCA9685 — LEDs on channels 0, 1, 2
#define LED_COUNT 3
Adafruit_PWMServoDriver pca = Adafruit_PWMServoDriver(0x40);

// LED state
uint8_t ledState[LED_COUNT]      = {0, 0, 0};
uint16_t ledBrightness[LED_COUNT] = {0, 0, 0};

WiFiServer server(TCP_PORT);
WiFiClient client;

unsigned long lastMsgMs    = 0;
unsigned long lastTelemetryMs = 0;
uint16_t telemetryInterval = 100;

// ── PCA9685 helpers ─────────────────────────────────────────────────────────
void setLed(uint8_t idx, uint8_t state, uint16_t brightness) {
  if (idx >= LED_COUNT) return;
  ledState[idx]      = state;
  ledBrightness[idx] = state ? brightness : 0;
  // PCA9685 is 12-bit (0–4095); scale from 0–255
  uint16_t pwm = state ? (uint16_t)(brightness * 4095UL / 255) : 0;
  pca.setPin(idx, pwm);
}

void allLedsOff() {
  for (uint8_t i = 0; i < LED_COUNT; i++) setLed(i, 0, 0);
}

// ── Send helpers ─────────────────────────────────────────────────────────────
void sendJson(JsonDocument& doc) {
  if (!client || !client.connected()) return;
  serializeJson(doc, client);
  client.print('\n');
}

void sendAck(const char* cmd) {
  StaticJsonDocument<64> doc;
  doc["type"] = "ACK";
  doc["cmd"]  = cmd;
  doc["ts"]   = millis();
  sendJson(doc);
}

void sendTelemetry() {
  StaticJsonDocument<256> doc;
  doc["type"]          = "STATE";
  doc["ts"]            = millis();
  // No encoders/motors in this minimal setup — send zeros
  doc["enc_motor1_deg"]  = 0.0;
  doc["enc_motor2_deg"]  = 0.0;
  doc["enc_led_arc_deg"] = 0.0;
  doc["enc_camera_deg"]  = 0.0;
  doc["motor1_moving"]   = false;
  doc["motor2_moving"]   = false;
  JsonArray states = doc.createNestedArray("led_states");
  JsonArray brights = doc.createNestedArray("led_brightness");
  for (uint8_t i = 0; i < LED_COUNT; i++) {
    states.add(ledState[i]);
    brights.add((uint8_t)ledBrightness[i]);
  }
  sendJson(doc);
}

// ── Command handler ──────────────────────────────────────────────────────────
void handleCommand(const char* line) {
  StaticJsonDocument<256> doc;
  if (deserializeJson(doc, line) != DeserializationError::Ok) {
    StaticJsonDocument<64> err;
    err["type"] = "NACK"; err["cmd"] = "?"; err["reason"] = "JSON parse error"; err["ts"] = millis();
    sendJson(err);
    return;
  }

  const char* type = doc["type"] | "";
  lastMsgMs = millis();

  if (strcmp(type, "PING") == 0) {
    StaticJsonDocument<64> r; r["type"] = "PONG"; r["ts"] = millis(); sendJson(r);

  } else if (strcmp(type, "GET_STATE") == 0) {
    sendTelemetry();

  } else if (strcmp(type, "LED_SET") == 0) {
    uint8_t idx = doc["index"] | 0;
    setLed(idx, doc["state"] | 0, doc["brightness"] | 200);
    sendAck("LED_SET");

  } else if (strcmp(type, "LED_ALL") == 0) {
    JsonArray states  = doc["states"];
    JsonArray brights = doc["brightness"];
    for (uint8_t i = 0; i < LED_COUNT && i < states.size(); i++)
      setLed(i, states[i], brights[i]);
    sendAck("LED_ALL");

  } else if (strcmp(type, "LED_OFF_ALL") == 0) {
    allLedsOff();
    sendAck("LED_OFF_ALL");

  } else if (strcmp(type, "SET_TELEMETRY") == 0) {
    telemetryInterval = constrain((uint16_t)(doc["interval_ms"] | 100), 50, 5000);
    sendAck("SET_TELEMETRY");

  } else if (strcmp(type, "STOP") == 0 || strcmp(type, "MOVE") == 0 ||
             strcmp(type, "JOG") == 0  || strcmp(type, "SET_HOME") == 0) {
    // No motors wired yet — just ACK so the UI doesn't time out
    sendAck(type);
  }
}

// ── Setup / Loop ─────────────────────────────────────────────────────────────
void setup() {
  Serial.begin(115200);

  Wire.begin();
  pca.begin();
  pca.setPWMFreq(1000);  // 1 kHz for LEDs
  allLedsOff();

  WiFi.begin(WIFI_SSID, WIFI_PASS);
  Serial.print("Connecting to WiFi");
  while (WiFi.status() != WL_CONNECTED) { delay(500); Serial.print('.'); }
  Serial.printf("\nConnected. IP: %s\n", WiFi.localIP().toString().c_str());

  server.begin();
  Serial.printf("TCP server listening on port %d\n", TCP_PORT);
  lastMsgMs = millis();
}

void loop() {
  // Accept new client
  if (!client || !client.connected()) {
    allLedsOff();
    client = server.available();
    if (client) {
      Serial.println("Client connected");
      lastMsgMs = millis();
    }
  }

  // Read incoming lines
  if (client && client.connected()) {
    while (client.available()) {
      String line = client.readStringUntil('\n');
      line.trim();
      if (line.length()) handleCommand(line.c_str());
    }

    // Watchdog: no message for 10 s → safe state
    if (millis() - lastMsgMs > 10000) {
      Serial.println("Watchdog timeout — all LEDs off");
      allLedsOff();
      lastMsgMs = millis();
    }

    // Periodic telemetry
    if (millis() - lastTelemetryMs >= telemetryInterval) {
      lastTelemetryMs = millis();
      sendTelemetry();
    }
  }
}
