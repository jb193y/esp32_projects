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
    # Only monitor dedicated BOOT / User button pins (avoid solenoid/relay GPIOs 3..21, 38..40)
    candidate_pins = [0, 47]
    button_pins = []

    for pin_num in candidate_pins:
        try:
            p = machine.Pin(pin_num, machine.Pin.IN, machine.Pin.PULL_UP)
            button_pins.append((pin_num, p))
        except Exception:
            pass

    print(f" Factory Reset monitor armed on {len(button_pins)} pins (GPIO {[p[0] for p in button_pins]}, 3s hold)")

    held_time = {pin_num: 0 for pin_num, _ in button_pins}

    while True:
        try:
            for pin_num, btn in button_pins:
                try:
                    val = btn.value()
                except Exception:
                    continue

                if val == 0:  # Active LOW (button pressed)
                    held_time[pin_num] += 100
                    if held_time[pin_num] == 800:
                        print(f" Factory Reset button (GPIO {pin_num}) hold detected... keep holding!")
                        try:
                            led_status.set_status("START_DISCOVERY")
                        except Exception:
                            pass

                    if held_time[pin_num] >= HOLD_TIME_MS:
                        print(f" Factory Reset triggered on GPIO {pin_num}! Resetting to BLE Setup mode...")
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
                            print(" Config reset notice:", ex)
                            try:
                                os.remove("config.json")
                            except Exception:
                                pass

                        time.sleep(1)
                        machine.reset()
                else:
                    held_time[pin_num] = 0
        except Exception as loop_ex:
            print(" Factory Reset monitor error:", loop_ex)

        time.sleep_ms(100)

def start():
    print(" Factory Reset monitor initializing...")
    try:
        _thread.start_new_thread(monitor_thread, ())
    except Exception as e:
        print(" Failed to start Factory Reset thread:", e)
