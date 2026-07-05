# main.py
import _thread
import time
import machine
import config
import mqtt
import pump_controller
import led_status
import network

# --- Thread Monitoring Heartbeats ---
heartbeats = {
    "pump": time.time()
}

def monitor_threads():
    """Safety Watchdog: Reboots the system if an active background thread hangs."""
    while True:
        time.sleep(10)
        now = time.time()
        
        lock_acquired = False
        try:
            # We check the ages of all registered heartbeats
            for thread_name, last_time in list(heartbeats.items()):
                age = now - last_time
                if age > 60:
                    print("🚨 CRITICAL: Thread hang detected on: %s! Age: %ds" % (thread_name, age))
                    time.sleep(1)
                    machine.reset()
        except Exception as e:
            print("⚠️ Watchdog scan error:", e)

print("🚀 main.py started - Submersible Pump Controller")

# Check setup button on boot for pairing override or factory reset
forced_ap_mode = False
try:
    # Load config to get the setup button pin (default to 0 / BOOT button)
    boot_cfg = config.load_config()
    setup_pin_num = boot_cfg.get("pump", {}).get("pins", {}).get("btn_setup", 0)
    
    # Initialize Pin (active-low, with pull-up)
    btn_setup = machine.Pin(setup_pin_num, machine.Pin.IN, machine.Pin.PULL_UP)
    
    # If button is pressed (value is 0) on boot
    if btn_setup.value() == 0:
        print("⏳ Checking Setup Button Boot Override...")
        held_count = 0
        # Wait up to 10 seconds (100 * 100ms)
        while btn_setup.value() == 0 and held_count < 100:
            time.sleep_ms(100)
            held_count += 1
            if held_count % 10 == 0:
                sec = held_count // 10
                if sec >= 10:
                    print("🚨 Hold button... %ds (FACTORY RESET TRIGGERED!)" % sec)
                elif sec >= 3:
                    print("⏳ Hold button... %ds (AP setup override active)" % sec)
                else:
                    print("⏳ Hold button... %ds" % sec)
            
        if held_count >= 100:
            print("🚨 FACTORY RESET ACTIVE: Wiping config.json and rebooting...")
            import os
            try:
                os.remove("config.json")
                print("✅ User configuration wiped!")
            except Exception as e:
                print("⚠️ config.json removal error (it may already be empty):", e)
                
            # Sound the buzzer for 2 seconds to confirm the factory reset
            try:
                buzzer_pin_num = boot_cfg.get("pump", {}).get("pins", {}).get("buzzer", 21)
                buzzer = machine.Pin(buzzer_pin_num, machine.Pin.OUT)
                buzzer.value(1)
                time.sleep(2.0)
                buzzer.value(0)
            except Exception:
                pass
                
            time.sleep(0.5)
            machine.reset()
        elif held_count >= 30:
            print("🚀 SETUP OVERRIDE ACTIVE: Forcing Access Point Setup Mode!")
            forced_ap_mode = True
        else:
            print("ℹ️ Button released too early. Standard boot continues.")
except Exception as e:
    print("⚠️ Setup button boot-check error:", e)

# 0. Start LED thread for status indication
_thread.start_new_thread(led_status.led_thread, ())

# Load configuration
cfg = config.load_config()
client_cfg = cfg.get("client", {})
mode = client_cfg.get("mode", "ap")

if forced_ap_mode:
    mode = "ap"

# Start Display Manager
if cfg.get("display", {}).get("enabled", True):
    try:
        import display_manager
        heartbeats["display"] = time.time()
        display_manager.start(heartbeats)
    except Exception as e:
        print("🚨 Failed to start display manager:", e)

# 1. Start Pump Controller Thread (Offline protection always runs)
_thread.start_new_thread(pump_controller.pump_thread, (heartbeats,))
time.sleep(2)

# 1.5. Start GPS Thread for location reporting
print("🛰️ Starting GPS thread...")
import gps
heartbeats["gps"] = time.time()
_thread.start_new_thread(gps.gps_thread, (heartbeats,))
time.sleep(1)

# 2. Check if Access Point is active (either AP mode or fallback)
ap = network.WLAN(network.AP_IF)
if ap.active() or mode == "ap":
    print("📡 Access Point active. Starting setup portal in background...")
    try:
        import server
        _thread.start_new_thread(server.start_server, ())
    except Exception as e:
        print("🚨 Failed to start background web server:", e)

# 3. Start MQTT Communication Thread if configured for STA mode
if mode == "sta":
    print("📶 STA Mode configured. Starting MQTT communication...")
    heartbeats["mqtt"] = time.time()
    _thread.start_new_thread(mqtt.mqtt_thread, (heartbeats,))

# 4. Run Watchdog
print("🛡️ System Monitor Active")
monitor_threads()