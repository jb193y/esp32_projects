# safety_monitor.py (Pump Controller)
import machine
import config

_estop_pin = None
_relay_pin = None
_on_estop_callback = None
is_estop_tripped = False

def handle_estop_irq(pin):
    global is_estop_tripped, _relay_pin, _on_estop_callback
    # 1. IMMEDIATELY open-circuit the contactor relay (write 0)
    if _relay_pin is not None:
         _relay_pin.value(0)
    
    is_estop_tripped = True
    
    # 2. Invoke callback to broadcast Alert (safely scheduled)
    if _on_estop_callback is not None:
        try:
            machine.schedule(_on_estop_callback, "E-Stop button pressed")
        except Exception:
            pass

def init_safety(relay_pin_instance, on_estop_callback_fn):
    global _estop_pin, _relay_pin, _on_estop_callback, is_estop_tripped
    _relay_pin = relay_pin_instance
    _on_estop_callback = on_estop_callback_fn
    is_estop_tripped = False
    
    cfg = config.load_config()
    pins = cfg.get("pump", {}).get("pins", {})
    estop_pin_num = pins.get("estop", 4)
    
    # E-Stop is active-low, pulled high. Trips when falling to 0 (pressed)
    _estop_pin = machine.Pin(estop_pin_num, machine.Pin.IN, machine.Pin.PULL_UP)
    
    # Check if estop is already pressed at startup
    if _estop_pin.value() == 0:
        is_estop_tripped = True
        if _relay_pin is not None:
            _relay_pin.value(0)
        print("⚠️ E-Stop is active on boot! Relay locked open.")
        
    # Register falling interrupt
    _estop_pin.irq(trigger=machine.Pin.IRQ_FALLING, handler=handle_estop_irq)
    print(f"🛡️ Safety Monitor configured on GPIO{estop_pin_num}")

def check_safety_state():
    global is_estop_tripped, _estop_pin
    if _estop_pin is not None and _estop_pin.value() == 0:
        is_estop_tripped = True
    return not is_estop_tripped

def reset_estop():
    global is_estop_tripped, _estop_pin
    if _estop_pin is not None and _estop_pin.value() == 1:
        is_estop_tripped = False
        print("🔓 E-Stop reset successfully.")
        return True
    print("⚠️ Cannot reset: E-Stop physical button is still pressed!")
    return False
