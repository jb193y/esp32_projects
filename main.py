import _thread
import time
import config
import gps
import mqtt

print("🚀 main.py started")

cfg = config.load_config()
mode = cfg.get('mode', 'ap')

if mode != 'sta':
    print("AP mode active — main services not started")
    # Keep alive, but do nothing
    while True:
        time.sleep(10)

print("STA mode detected — starting services")

# Start GPS thread
_thread.start_new_thread(gps.gps_thread, ())
time.sleep(2)

# Start MQTT thread
_thread.start_new_thread(mqtt.mqtt_thread, ())

# Keep main alive
while True:
    time.sleep(5)
