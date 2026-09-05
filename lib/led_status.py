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
    "START_DISCOVERY": ((200, 200), (200, 200)),  # Rapid discovery blink (Magenta)
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

# Color palette for NeoPixel RGB LEDs (Distinct visual indicator for each state)
NEO_COLORS = {
    "BLE_PROVISIONING": (0, 0, 255),    # Blue (Blinking - BLE setup pairing mode)
    "BLE_CONNECTED": (0, 255, 255),     # Cyan / Aqua (Pulse - Mobile app connected)
    "START_DISCOVERY": (255, 0, 255),   # Magenta / Purple (Rapid Blink - Mesh Discovery mode active)
    "WIFI_CONNECTING": (255, 128, 0),   # Amber / Orange (Fast Pulse - Connecting to Wi-Fi)
    "WIFI_CONNECTED": (255, 255, 0),    # Yellow (Pulse - Wi-Fi connected, reaching MQTT)
    "MQTT_CONNECTED": (0, 255, 0),      # Solid Green (Fully online & operational)
    "VALVE_OPEN": (0, 255, 128),        # Lime Green (Pulse - Solenoid Valve open/active)
    "VALVE_CLOSED": (15, 15, 0),        # Soft Warm Dim (Idle standby)
    "NORMAL_OFF": (0, 15, 15),          # Soft Teal Dim (Normal mode quiet idle)
    "RUNNING": (0, 255, 0),             # Pure Green (Operational)
    "FAULT": (255, 0, 0),               # Red (Blink/Solid - Fault alarm)
    "RESTART_DELAY": (255, 64, 0),      # Coral Deep Orange (Blink - Restart countdown)
    "OFF": (0, 0, 0),                   # Off
}

def led_thread():
    print(" Universal LED Status thread started")
    cfg = config.load_config()
    client_cfg = cfg.get("client", {})
    client_type = client_cfg.get("type", "client").lower()
    
    # 1. NeoPixel RGB LED configuration
    np = None
    neo_used_pin = None
    neopixel_pin = client_cfg.get("neopixel_pin")
    if neopixel_pin is None:
        board = str(client_cfg.get("board_type", "")).upper()
        if "S3" in board:
            neopixel_pin = 48

    if has_neopixel and neopixel_pin is not None:
        try:
            np = neopixel.NeoPixel(Pin(int(neopixel_pin)), 1)
            neo_used_pin = int(neopixel_pin)
        except Exception:
            pass

    # 2. Single-Color Status LED configuration (explicit only, no blind probing)
    active_led_pins = []
    status_pin_num = client_cfg.get("status_led")
    if status_pin_num is not None:
        try:
            status_pin_num = int(status_pin_num)
            if status_pin_num != neo_used_pin:
                active_led_pins.append(Pin(status_pin_num, Pin.OUT))
        except Exception as e:
            print(f" Failed to initialize status_led on Pin {status_pin_num}: {e}")

    # Fallback to GPIO 2 only on classic non-S3 ESP32 DevKits if nothing configured
    if np is None and not active_led_pins:
        board = str(client_cfg.get("board_type", "")).upper()
        if "S3" not in board and client_type != "valve":
            try:
                active_led_pins.append(Pin(2, Pin.OUT))
            except Exception:
                pass

    print(f" Universal LED Status Mode active: {len(active_led_pins)} GPIO pins, NeoPixel={np is not None} (Pin {neo_used_pin})")

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

        neo_rgb = NEO_COLORS.get(current_state, (0, 30, 0))

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
