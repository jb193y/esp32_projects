# main.py
import _thread
import time
import machine
import config
import gps
import mqtt
import imu

# --- Thread Monitoring Heartbeats ---
# Shared dictionary to track when threads were last active
heartbeats = {
    "gps": time.time(),
    "mqtt": time.time()
}

def monitor_threads():
    """Safety Watchdog: Reboots the system if a background thread hangs."""
    while True:
        time.sleep(10)
        now = time.time()
        
        # Check if GPS thread is alive (expected update every few seconds)
        gps_age = now - heartbeats["gps"]
        # Check if MQTT thread is alive
        mqtt_age = now - heartbeats["mqtt"]
        
        if gps_age > 60 or mqtt_age > 60:
            print("🚨 CRITICAL: Thread hang detected!")
            print("GPS Age: %ds, MQTT Age: %ds" % (gps_age, mqtt_age))
            print("🔄 Rebooting system...")
            time.sleep(1)
            machine.reset()

print("🚀 main.py started")

# Load configuration
cfg = config.load_config()
mode = cfg.get("device", {}).get("mode", "ap")

if mode != "sta":
    print("📡 AP mode active — main services not started. Waiting for configuration.")
    # In AP mode, the server.py (called from boot.py) handles everything.
    while True:
        time.sleep(10)

print("✅ STA mode detected — Initializing Hardware")

# 1. Initialize IMU (Safely before threading starts)
imu.init_imu()

# 2. Start GPS Thread (Pass heartbeats for monitoring)
_thread.start_new_thread(gps.gps_thread, (heartbeats,))
time.sleep(2)

# 3. Start MQTT Thread (Pass heartbeats for monitoring)
_thread.start_new_thread(mqtt.mqtt_thread, (heartbeats,))

# 4. Run Watchdog Monitor (in the main thread)
print("🛡️ Thread Watchdog active")
monitor_threads()