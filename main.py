# main.py
import _thread
import time
import machine
import config
import gps
import mqtt
import imu

# --- Thread Monitoring Heartbeats ---
heartbeats = {
    "gps": time.time(),
    "mqtt": time.time()
}

def monitor_threads():
    """Safety Watchdog: Reboots the system if a background thread hangs."""
    while True:
        time.sleep(10)
        now = time.time()
        
        gps_age = now - heartbeats["gps"]
        mqtt_age = now - heartbeats["mqtt"]
        
        if gps_age > 60 or mqtt_age > 60:
            print("🚨 CRITICAL: Thread hang detected!")
            print("GPS Age: %ds, MQTT Age: %ds" % (gps_age, mqtt_age))
            time.sleep(1)
            machine.reset()

print("🚀 main.py started")

# Load configuration
cfg = config.load_config()
client_cfg = cfg.get("client", {})
mode = client_cfg.get("mode", "ap")
client_type = client_cfg.get("type", "rover")

if mode != "sta":
    print("📡 AP mode active — waiting for configuration.")
    while True:
        time.sleep(10)

print(f"✅ STA mode: {client_type.upper()} initialization")

# 1. Selective Hardware Init
if client_type == "rover":
    print("🧭 Initializing IMU for Rover mode...")
    imu_present = imu.init_imu()
    if not imu_present:
        print("⚠️ Warning: Rover starting without IMU (GPS-only)")
else:
    print("📍 Base Mode: Skipping IMU initialization.")

# 2. Start GPS Thread
# Ensure gps.gps_thread is updated to: def gps_thread(heartbeats=None):
_thread.start_new_thread(gps.gps_thread, (heartbeats,))
time.sleep(2)

# 3. Start MQTT Thread
# Ensure mqtt.mqtt_thread is updated to: def mqtt_thread(heartbeats=None):
_thread.start_new_thread(mqtt.mqtt_thread, (heartbeats,))

# 4. Run Watchdog
print("🛡️ System Monitor Active")
monitor_threads()