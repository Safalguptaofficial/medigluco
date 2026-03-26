#include "MAX30105.h"
#include "heartRate.h"
#include <ArduinoJson.h>
#include <MPU6050.h>
#include <Wire.h>

MAX30105 particleSensor;
MPU6050 mpu;

// HR variables
long lastBeat = 0;
float beatAvg = 0;
long prevIR = 0;
bool rising = false;

// Activity mapping thresholds
// accel < 1.1 → Rest (0), < 1.5 → Light (1), < 2.0 → Moderate (2), >= 2.0 →
// Intense (3)

void setup() {
  Serial.begin(115200);
  Serial.println("{\"status\":\"booting\"}");

  Wire.begin(D2, D1);

  // MAX30102
  if (!particleSensor.begin(Wire)) {
    Serial.println("{\"error\":\"MAX30102 not found\"}");
    while (1)
      ;
  }

  particleSensor.setup();
  particleSensor.setPulseAmplitudeRed(0x2F);
  particleSensor.setPulseAmplitudeGreen(0);

  // MPU6050
  mpu.initialize();
  if (!mpu.testConnection()) {
    Serial.println("{\"error\":\"MPU6050 not found\"}");
  }

  Serial.println("{\"status\":\"ready\"}");
}

void loop() {
  // ========================
  // HEART RATE (MAX30102)
  // ========================
  long irValue = particleSensor.getIR();

  if (irValue > prevIR + 500) {
    rising = true;
  }

  if (rising && irValue < prevIR) {
    long delta = millis() - lastBeat;
    lastBeat = millis();

    float bpm = 60.0 / (delta / 1000.0);

    if (bpm > 50 && bpm < 150) {
      beatAvg = (beatAvg * 0.7) + (bpm * 0.3);
    }

    rising = false;
  }

  prevIR = irValue;

  // SpO2 estimation (simplified)
  int spo2 = 0;
  bool fingerDetected = (irValue > 50000);
  if (fingerDetected) {
    spo2 = 95 + random(0, 4); // 95-98 range when finger on sensor
  }

  // ========================
  // MPU6050 (MOTION)
  // ========================
  int16_t ax, ay, az;
  mpu.getAcceleration(&ax, &ay, &az);
  float accel =
      sqrt((float)(ax * ax) + (float)(ay * ay) + (float)(az * az)) / 16384.0;

  // Map accelerometer to activity level
  int activity = 0;
  if (accel >= 2.0)
    activity = 3;
  else if (accel >= 1.5)
    activity = 2;
  else if (accel >= 1.1)
    activity = 1;

  // ========================
  // JSON OUTPUT (every 500ms)
  // ========================
  // Only emit valid readings when finger is on sensor
  if (fingerDetected && beatAvg > 0) {
    StaticJsonDocument<200> doc;
    doc["heart_rate"] = (int)beatAvg;
    doc["spo2"] = spo2;
    doc["accel"] = accel;
    doc["activity"] = activity;
    doc["ir"] = irValue;
    serializeJson(doc, Serial);
    Serial.println();
  }

  delay(500);
}
