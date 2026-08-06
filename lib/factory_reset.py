# lib/factory_reset.py
import machine
import time
import _thread
import os
import ujson
import led_status
import config

HOLD_TIME_MS = 3000  # 3-second button hold for factory reset

def monitor_thread():
    candidate_pins = [0, 48, 9, 47, 38, 21, 14]
    button_pins = []

    for pin_num in candidate_pins:
        try:
            p = machine.Pin(pin_num, machine.Pin.IN, machine.Pin.PULL_UP)
            button_pins.append((pin_num, p))
        except Exception:
            pass

    print(f" Factory Reset monitor armed on {len(button_pins)} pins (3s hold)")

    while True:
        for pin_num, btn in button_pins:
            if btn.value() == 0:  # Button pressed
                press_start = time.ticks_ms()
                while btn.value() == 0:
                    held_ms = time.ticks_diff(time.ticks_ms(), press_start)
                    if held_ms > 800:
                        try:
                            led_status.set_status("BLE_PROVISIONING")
                        except Exception:
                            pass

                    if held_ms >= HOLD_TIME_MS:
                        print(f" Factory Reset Button held for 3s on GPIO {pin_num}!")
                        print(" Setting mode to ble_setup in config.json...")
                        try:
                            cfg = config.load_config()
                            cfg.setdefault("client", {})["mode"] = "ble_setup"
                            with open("config.json", "w") as f:
                                ujson.dump(cfg, f)
                        except Exception:
                            try:
                                os.remove("config.json")
                            except Exception:
                                pass
                        
                        print(" Rebooting to BLE Provisioning mode...")
                        time.sleep(1)
                        machine.reset()
                    time.sleep_ms(100)

        time.sleep_ms(200)

def start():
    print(" Factory Reset monitor initializing...")
    _thread.start_new_thread(monitor_thread, ())
