import _thread
import time
import config
import gps
import mqtt
import imu

print("🚀 main.py started")

cfg = config.load_config()
mode = cfg.get("device", {}).get("mode", "ap")

if mode != "sta":
    print("AP mode active — main services not started")
    while True:
        time.sleep(10)

print("STA mode detected — initializing hardware")

# 🔹 Initialize IMU FIRST (safe)
imu.init_imu()

# 🔹 Start GPS
_thread.start_new_thread(gps.gps_thread, ())
time.sleep(2)

# 🔹 Start MQTT
_thread.start_new_thread(mqtt.mqtt_thread, ())

while True:
    time.sleep(5)
