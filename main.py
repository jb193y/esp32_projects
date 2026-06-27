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

# 0. Start LED thread for status indication
_thread.start_new_thread(led_status.led_thread, ())

# Load configuration
cfg = config.load_config()
client_cfg = cfg.get("client", {})
mode = client_cfg.get("mode", "ap")

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