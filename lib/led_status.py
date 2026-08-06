# led_status.py (Shared Universal LED Status Library)
import _thread
import time
from machine import Pin
import config

try:
    import neopixel
    has_neopixel = True
except ImportError:
    has_neopixel = False

_state = "OFF"
_lock = _thread.allocate_lock()

def set_status(new_status):
    global _state
    _lock.acquire()
    try:
        if _state != new_status:
            print(f" LED Status -> {new_status}")
            _state = new_status
    finally:
        _lock.release()

# Patterns: ((Run_ON, Run_OFF), (Fault_ON, Fault_OFF)) in ms
PATTERNS = {
    "OFF": ((0, 1000), (0, 1000)),
    "BLE_PROVISIONING": ((150, 150), (150, 150)),  # Rapid pairing blink
    "BLE_CONNECTED": ((400, 100), (400, 100)),     # High-duty pulse when phone connected
    "WIFI_CONNECTING": ((100, 100), (0, 1000)),
    "WIFI_CONNECTED": ((100, 1900), (0, 1000)),   # Short pulse  Wi-Fi up, MQTT pending
    "MQTT_CONNECTED": ((1000, 0), (0, 1000)),     # Solid ON  fully operational
    "NORMAL_OFF": ((100, 2900), (0, 1000)),
    "VALVE_CLOSED": ((100, 2900), (0, 1000)),
    "VALVE_OPEN": ((1000, 1000), (0, 1000)),
    "RUNNING": ((1000, 0), (0, 1000)),
    "FAULT": ((0, 1000), (1000, 0)),
    "RESTART_DELAY": ((500, 500), (0, 1000)),
}

# Color palette for NeoPixel RGB LEDs
NEO_COLORS = {
    "BLE_PROVISIONING": (0, 0, 255),    # Blue
    "BLE_CONNECTED": (0, 255, 255),     # Cyan
    "WIFI_CONNECTING": (255, 165, 0),   # Amber/Orange
    "WIFI_CONNECTED": (0, 255, 0),      # Green
    "MQTT_CONNECTED": (0, 255, 0),      # Green
    "VALVE_OPEN": (0, 255, 0),          # Green
    "VALVE_CLOSED": (50, 50, 50),       # Dim White
    "RUNNING": (0, 255, 0),             # Green
    "FAULT": (255, 0, 0),               # Red
    "OFF": (0, 0, 0),
}

def led_thread():
    print(" Universal LED Status thread started")
    cfg = config.load_config()
    client_cfg = cfg.get("client", {})
    client_type = client_cfg.get("type", "client").lower()
    
    pins_cfg = cfg.get(client_type, {}).get("pins", {})
    if not pins_cfg:
        pins_cfg = client_cfg.get("pins", {})
        
    status_pin_num = pins_cfg.get("status_led")

    # Candidate GPIO pins for standard LEDs across ESP32 / ESP32-S3 boards
    candidate_pins = [2, 21, 38, 47, 48]
    if status_pin_num is not None and status_pin_num not in candidate_pins:
        candidate_pins.insert(0, status_pin_num)

    active_led_pins = []
    for pin_num in candidate_pins:
        try:
            active_led_pins.append(Pin(pin_num, Pin.OUT))
        except Exception:
            pass

    # Initialize NeoPixel RGB on GPIO 48 / 38 if available
    np = None
    if has_neopixel:
        for neo_pin in [48, 38]:
            try:
                np = neopixel.NeoPixel(Pin(neo_pin), 1)
                break
            except Exception:
                pass

    print(f" Universal LED Status Mode active: {len(active_led_pins)} GPIO pins, NeoPixel={np is not None}")

    while True:
        _lock.acquire()
        try:
            current_state = _state
        finally:
            _lock.release()

        run_pat, fault_pat = PATTERNS.get(current_state, ((100, 900), (0, 1000)))
        
        if current_state == "FAULT":
            on_ms, off_ms = fault_pat
        else:
            on_ms, off_ms = run_pat

        neo_rgb = NEO_COLORS.get(current_state, (0, 0, 255))

        if on_ms > 0:
            # Turn ON standard LEDs
            for p in active_led_pins:
                try:
                    p.value(1)
                except Exception:
                    pass

            # Turn ON NeoPixel RGB LED
            if np:
                try:
                    np[0] = neo_rgb
                    np.write()
                except Exception:
                    pass

            time.sleep_ms(on_ms)

        if off_ms > 0:
            # Turn OFF standard LEDs
            for p in active_led_pins:
                try:
                    p.value(0)
                except Exception:
                    pass

            # Turn OFF NeoPixel RGB LED
            if np:
                try:
                    np[0] = (0, 0, 0)
                    np.write()
                except Exception:
                    pass

            time.sleep_ms(off_ms)
