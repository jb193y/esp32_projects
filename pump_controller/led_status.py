# led_status.py (Pump Controller)
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
    "BLE_PROVISIONING": ((200, 200), (200, 200)), # both flash
    "NORMAL_OFF": ((100, 2900), (0, 1000)),       # short run pulse
    "RUNNING": ((1000, 0), (0, 1000)),             # solid run
    "FAULT": ((0, 1000), (1000, 0)),               # solid fault
    "RESTART_DELAY": ((500, 500), (0, 1000)),      # slow blinking run
}

def led_thread():
    print("✅ LED Status thread started")
    cfg = config.load_config()
    pins_cfg = cfg.get("pump", {}).get("pins", {})
    run_pin_num = pins_cfg.get("led_run", 2)
    fault_pin_num = pins_cfg.get("led_fault", 12)
    
    led_run = Pin(run_pin_num, Pin.OUT)
    led_fault = Pin(fault_pin_num, Pin.OUT)
    
    while True:
        with _lock:
            current_state = _state
            
        run_pat, fault_pat = PATTERNS.get(current_state, ((100, 900), (0, 1000)))
        
        run_on, run_off = run_pat
        fault_on, fault_off = fault_pat
        
        # Turn ON
        if run_on > 0: led_run.value(1)
        if fault_on > 0: led_fault.value(1)
        
        max_on = max(run_on, fault_on)
        if max_on > 0:
            time.sleep_ms(max_on)
            
        # Turn OFF
        if run_on > 0: led_run.value(0)
        if fault_on > 0: led_fault.value(0)
        
        max_off = max(run_off, fault_off)
        if max_off > 0:
            time.sleep_ms(max_off)
