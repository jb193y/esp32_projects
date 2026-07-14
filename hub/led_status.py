# led_status.py (Hub)
import _thread
import time
from machine import Pin

# Default status LED pin on ESP32 is usually Pin 2
LED_PIN = 2

_led = Pin(LED_PIN, Pin.OUT)
_state = "OFF"
_lock = _thread.allocate_lock()

def set_status(new_status):
    """Thread-safe status change for LED blinking patterns."""
    global _state
    with _lock:
        if _state != new_status:
            print(f"💡 LED Status -> {new_status}")
            _state = new_status

PATTERNS = {
    "OFF": (0, 1000),
    "BLE_PROVISIONING": (200, 200),   # Alternate fast blink
    "WIFI_CONNECTING": (100, 100),   # Very fast blink
    "WIFI_CONNECTED": (100, 1900),   # Short pulse
    "MQTT_CONNECTED": (1000, 0),     # Solid ON
}

def led_thread():
    print("✅ LED Status thread started")
    while True:
        with _lock:
            current_state = _state
        
        on_ms, off_ms = PATTERNS.get(current_state, (50, 950))

        if on_ms > 0:
            _led.value(1)
            time.sleep_ms(on_ms)
        
        if off_ms > 0:
            _led.value(0)
            time.sleep_ms(off_ms)
