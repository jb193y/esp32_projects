# led_status.py (Shared LED Status Library)
import _thread
import time
from machine import Pin
import config

_state = "OFF"
_lock = _thread.allocate_lock()

def set_status(new_status):
    global _state
    with _lock:
        if _state != new_status:
            print(f"💡 LED Status -> {new_status}")
            _state = new_status

# Patterns: ((Run_ON, Run_OFF), (Fault_ON, Fault_OFF)) in ms
PATTERNS = {
    "OFF": ((0, 1000), (0, 1000)),
    "BLE_PROVISIONING": ((200, 200), (200, 200)),
    "WIFI_CONNECTING": ((100, 100), (0, 1000)),
    "NORMAL_OFF": ((100, 2900), (0, 1000)),
    "VALVE_CLOSED": ((100, 2900), (0, 1000)),
    "VALVE_OPEN": ((1000, 1000), (0, 1000)),
    "RUNNING": ((1000, 0), (0, 1000)),
    "FAULT": ((0, 1000), (1000, 0)),
    "RESTART_DELAY": ((500, 500), (0, 1000)),
}

def led_thread():
    print("✅ LED Status thread started")
    cfg = config.load_config()
    client_cfg = cfg.get("client", {})
    client_type = client_cfg.get("type", "client").lower()
    
    # Try type-specific pins first, then generic pins
    pins_cfg = cfg.get(client_type, {}).get("pins", {})
    if not pins_cfg:
        pins_cfg = client_cfg.get("pins", {})
        
    run_pin_num = pins_cfg.get("led_run")
    fault_pin_num = pins_cfg.get("led_fault")
    status_pin_num = pins_cfg.get("status_led", 2) # default to GPIO 2 status LED
    
    # Detect mode
    is_dual = (run_pin_num is not None) and (fault_pin_num is not None) and (run_pin_num != fault_pin_num)
    
    if is_dual:
        print(f"💡 Dual LED Mode initialized: RUN={run_pin_num}, FAULT={fault_pin_num}")
        led_run = Pin(run_pin_num, Pin.OUT)
        led_fault = Pin(fault_pin_num, Pin.OUT)
        
        while True:
            with _lock:
                current_state = _state
                
            run_pat, fault_pat = PATTERNS.get(current_state, ((100, 900), (0, 1000)))
            run_on, run_off = run_pat
            fault_on, fault_off = fault_pat
            
            if run_on > 0: led_run.value(1)
            if fault_on > 0: led_fault.value(1)
            
            max_on = max(run_on, fault_on)
            if max_on > 0:
                time.sleep_ms(max_on)
                
            if run_on > 0: led_run.value(0)
            if fault_on > 0: led_fault.value(0)
            
            max_off = max(run_off, fault_off)
            if max_off > 0:
                time.sleep_ms(max_off)
    else:
        print(f"💡 Single LED Mode initialized on GPIO {status_pin_num}")
        led_status = Pin(status_pin_num, Pin.OUT)
        
        while True:
            with _lock:
                current_state = _state
                
            run_pat, fault_pat = PATTERNS.get(current_state, ((100, 900), (0, 1000)))
            
            # For single LED, use fault pattern if state is FAULT, otherwise run pattern
            if current_state == "FAULT":
                on_ms, off_ms = fault_pat
            else:
                on_ms, off_ms = run_pat
                
            if on_ms > 0:
                led_status.value(1)
                time.sleep_ms(on_ms)
            if off_ms > 0:
                led_status.value(0)
                time.sleep_ms(off_ms)
