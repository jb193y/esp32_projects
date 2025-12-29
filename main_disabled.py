import _thread
import time
import gps
import mqtt

print("🚀 System starting...")

_thread.start_new_thread(gps.gps_thread, ())
time.sleep(2)
_thread.start_new_thread(mqtt.mqtt_thread, ())

# Keep main alive
while True:
    time.sleep(5)
