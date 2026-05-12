#include <Arduino.h>
#include "soc/gpio_reg.h"
#include "soc/gpio_struct.h"

// ============================================================
// Generador patrón MSO para probar driver / PulseView
// ESP32 clásico con DAC en GPIO25/GPIO26
// ============================================================
//
// ANA0 / CH1: GPIO25 DAC1 -> diente de sierra
// ANA1 / CH2: GPIO26 DAC2 -> triangular
//
// DIG0..DIG5: contador binario de 6 bits
//
// Tick global: 100 us
//
// Frecuencias digitales:
//   DIG0 = 5 kHz
//   DIG1 = 2.5 kHz
//   DIG2 = 1.25 kHz
//   DIG3 = 625 Hz
//   DIG4 = 312.5 Hz
//   DIG5 = 156.25 Hz
//
// Ondas analógicas:
//   Saw:      ~6.4 ms/ciclo
//   Triangle: ~12.8 ms/ciclo
//
// ============================================================

// -------------------- DAC analógicos --------------------

const uint8_t PIN_DAC_SAW = 25;  // DAC1
const uint8_t PIN_DAC_TRI = 26;  // DAC2

// DAC ESP32 clásico: 8 bits, 0..255
const uint8_t DAC_STEP = 4;

// -------------------- GPIO digitales --------------------
//
// Todos por debajo de GPIO32 para poder usar GPIO.out_w1ts/out_w1tc.
// Si cambias a GPIO >= 32, habría que usar los registros out1.

const uint8_t digitalPins[6] = {
  13,  // DIG0 - bit 0, más rápido
  14,  // DIG1 - bit 1
  16,  // DIG2 - bit 2
  17,  // DIG3 - bit 3
  18,  // DIG4 - bit 4
  19   // DIG5 - bit 5, más lento
};

uint32_t digitalBitMask[6];
uint32_t digitalAllMask = 0;

// -------------------- Timing --------------------

const uint32_t TICK_US = 100;  // 10 kHz de actualización global

uint32_t nextTick = 0;

// -------------------- Estado interno --------------------

uint32_t counterValue = 0;

uint8_t sawValue = 0;

int16_t triValue = 0;
int16_t triDirection = DAC_STEP;

// ============================================================
// Escritura rápida de los 6 canales digitales
// ============================================================

void writeDigitalCounter(uint32_t value) {
  uint32_t setMask = 0;

  for (uint8_t i = 0; i < 6; i++) {
    if ((value >> i) & 0x01) {
      setMask |= digitalBitMask[i];
    }
  }

  uint32_t clearMask = digitalAllMask & ~setMask;

  REG_WRITE(GPIO_OUT_W1TC_REG, clearMask);  // clear bits
  REG_WRITE(GPIO_OUT_W1TS_REG, setMask);    // set bits
}

// ============================================================
// Actualización sincronizada de analógico + digital
// ============================================================

void updatePattern() {
  // --------------------
  // Digital: contador binario
  // --------------------

  counterValue++;
  writeDigitalCounter(counterValue);

  // --------------------
  // Analógico 0: diente de sierra
  // --------------------

  sawValue = sawValue + DAC_STEP;  // uint8_t, hace wrap automáticamente 252 -> 0
  dacWrite(PIN_DAC_SAW, sawValue);

  // --------------------
  // Analógico 1: triangular
  // --------------------

  triValue += triDirection;

  if (triValue >= 255) {
    triValue = 255;
    triDirection = -DAC_STEP;
  } else if (triValue <= 0) {
    triValue = 0;
    triDirection = DAC_STEP;
  }

  dacWrite(PIN_DAC_TRI, (uint8_t)triValue);
}

// ============================================================
// Arduino setup
// ============================================================

void setup() {
  // No hace falta Serial. Lo dejamos apagado para no meter ruido conceptual.

  // Configurar digitales
  for (uint8_t i = 0; i < 6; i++) {
    pinMode(digitalPins[i], OUTPUT);
    digitalWrite(digitalPins[i], LOW);

    digitalBitMask[i] = (1UL << digitalPins[i]);
    digitalAllMask |= digitalBitMask[i];
  }

  // Inicializar DACs
  dacWrite(PIN_DAC_SAW, 0);
  dacWrite(PIN_DAC_TRI, 0);

  nextTick = micros();
}

// ============================================================
// Arduino loop
// ============================================================

void loop() {
  uint32_t now = micros();

  if ((int32_t)(now - nextTick) >= 0) {
    nextTick += TICK_US;
    updatePattern();
  }
}
