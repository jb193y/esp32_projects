# lib/factory_reset.py
import machine
import time
import os
import ujson
import micropython
import led_status
import config

HOLD_TIME_MS = 3000  # 3-second button hold for factory reset
_timer = None
_held_time = {}
_button_pins = []
_reset_scheduled = False

def _do_factory_reset(pin_num):
    """Executes outside ISR context where heap allocation and file I/O are fully permitted."""
    global _reset_scheduled
    try:
        print(f"\r\n--- FACTORY RESET TRIGGERED (GPIO {pin_num}) ---")
        try:
            led_status.set_status("BLE_PROVISIONING")
        except Exception:
            pass

        try:
            cfg = config.load_config()
            cfg.setdefault("client", {})["mode"] = "ble_setup"
            with open("config.json", "w") as f:
                ujson.dump(cfg, f)
            print(" Updated config.json to mode: ble_setup")
        except Exception as ex:
            print(" Config reset error:", ex)
            try:
                os.remove("config.json")
                print(" Removed config.json")
            except Exception:
                pass

        try:
            import sys
            sys.stdout.write("\r\n--- FACTORY RESET COMPLETE: SOFT REBOOTING ESP32 ---\r\n")
            sys.stdout.flush()
        except Exception:
            pass

        time.sleep_ms(400)
        machine.soft_reset()
    except Exception as err:
        print("Factory reset fatal error:", err)
        _reset_scheduled = False

def _check_buttons(t):
    global _held_time, _button_pins, _reset_scheduled
    if _reset_scheduled:
        return

    for pin_num, btn in _button_pins:
        try:
            if btn.value() == 0:  # Active LOW (button pressed)
                _held_time[pin_num] = _held_time.get(pin_num, 0) + 100

                if _held_time[pin_num] >= HOLD_TIME_MS:
                    _reset_scheduled = True
                    try:
                        micropython.schedule(_do_factory_reset, pin_num)
                    except Exception:
                        pass
            else:
                _held_time[pin_num] = 0
        except Exception:
            pass

def start(custom_pins=None):
    global _timer, _held_time, _button_pins, _reset_scheduled
    _reset_scheduled = False
    print(" Factory Reset monitor initializing (Timer mode)...")
    candidate_pins = custom_pins if custom_pins is not None else [0, 47]
    _button_pins = []
    for pin_num in candidate_pins:
        try:
            p = machine.Pin(pin_num, machine.Pin.IN, machine.Pin.PULL_UP)
            _button_pins.append((pin_num, p))
        except Exception:
            pass

    _held_time = {pin_num: 0 for pin_num, _ in _button_pins}
    try:
        _timer = machine.Timer(0)
        _timer.init(period=100, mode=machine.Timer.PERIODIC, callback=_check_buttons)
        print(f" Factory Reset timer (0) armed on {len(_button_pins)} pins (GPIO {[p[0] for p in _button_pins]}, 3s hold)")
    except Exception:
        try:
            _timer = machine.Timer(-1)
            _timer.init(period=100, mode=machine.Timer.PERIODIC, callback=_check_buttons)
            print(f" Factory Reset timer (-1) armed on {len(_button_pins)} pins (3s hold)")
        except Exception as e:
            print(" Failed to initialize Factory Reset timer:", e)
