# lib/factory_reset.py
import machine
import time
import _thread
import os

BUTTON_PIN = 0  # Standard ESP32 BOOT button
HOLD_TIME_MS = 5000

def monitor_thread():
    button = machine.Pin(BUTTON_PIN, machine.Pin.IN, machine.Pin.PULL_UP)
    
    # Wait until button is released (HIGH) before arming reset detection
    # This prevents continuous reset loops if GPIO 0 is LOW during boot/powerup
    while button.value() == 0:
        time.sleep_ms(500)
    
    print("✅ Factory Reset button listener armed (waiting for 5s hold on GPIO 0)")

    while True:
        if button.value() == 0:  # Button is pressed
            press_start = time.ticks_ms()
            while button.value() == 0:
                if time.ticks_diff(time.ticks_ms(), press_start) > HOLD_TIME_MS:
                    print("⚠️ Factory Reset Button held for 5 seconds!")
                    print("🧹 Wiping config.json...")
                    try:
                        os.remove("config.json")
                    except OSError:
                        pass
                    print("🔄 Rebooting to BLE Provisioning mode...")
                    time.sleep(1)
                    machine.reset()
                time.sleep_ms(100)
        time.sleep_ms(200)

def start():
    print(f"🔄 Factory Reset monitor started on GPIO {BUTTON_PIN}")
    _thread.start_new_thread(monitor_thread, ())
