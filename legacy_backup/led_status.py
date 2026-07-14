# led_status.py
import _thread
import time
from machine import Pin

# --- Configuration ---
# Default is 2 for many ESP32 boards. Change if your board is different.
LED_PIN = 2

# --- State Management ---
_led = Pin(LED_PIN, Pin.OUT)
_state = "OFF"
_lock = _thread.allocate_lock()

# --- Public API ---
def set_status(new_status):
    """Thread-safe method to change the LED's blinking pattern."""
    global _state
    with _lock:
        if _state != new_status:
            print(f"💡 LED Status -> {new_status}")
            _state = new_status

# --- Patterns: (duration_on_ms, duration_off_ms) ---
PATTERNS = {
    "OFF": (0, 1000),
    "AP_MODE": (200, 2800),           # Slow pulse for AP mode
    "WIFI_CONNECTING": (100, 100),     # Fast blink
    "WIFI_CONNECTED": (50, 2950),      # Slow "heartbeat"
    "MQTT_CONNECTED": (1000, 0),       # Solid ON
    "OTA_UPDATE": (50, 50),            # Very fast blink for OTA
}

# --- Thread ---
def led_thread():
    print("✅ LED Status thread started")
    while True:
        with _lock:
            current_state = _state
        
        on_ms, off_ms = PATTERNS.get(current_state, (25, 975)) # Default to a quick blink

        if on_ms > 0:
            _led.value(1)
            time.sleep_ms(on_ms)
        
        if off_ms > 0:
            _led.value(0)
            time.sleep_ms(off_ms)