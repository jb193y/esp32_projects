# main.py
import _thread
import time
import machine
import config
import mqtt
import pump_controller
import led_status

# --- Thread Monitoring Heartbeats ---
heartbeats = {
    "pump": time.time(),
    "mqtt": time.time()
}

def monitor_threads():
    """Safety Watchdog: Reboots the system if a background thread hangs."""
    while True:
        time.sleep(10)
        now = time.time()
        
        pump_age = now - heartbeats["pump"]
        mqtt_age = now - heartbeats["mqtt"]
        
        if pump_age > 60 or mqtt_age > 60:
            print("🚨 CRITICAL: Thread hang detected!")
            print("Pump Thread Age: %ds, MQTT Thread Age: %ds" % (pump_age, mqtt_age))
            time.sleep(1)
            machine.reset()

print("🚀 main.py started - Submersible Pump Controller")

# 0. Start LED thread for status indication
_thread.start_new_thread(led_status.led_thread, ())

# Load configuration
cfg = config.load_config()
client_cfg = cfg.get("client", {})
mode = client_cfg.get("mode", "ap")

if mode != "sta":
    print("📡 AP mode active — waiting for configuration.")
    # In AP mode, start the local setup server
    try:
        import server
        server.start_server()
    except Exception as e:
        print("🚨 Failed to start AP server:", e)
    while True:
        time.sleep(10)

print("✅ STA mode: Running Pump Controller initialization")

# 1. Start Pump Controller Thread
_thread.start_new_thread(pump_controller.pump_thread, (heartbeats,))
time.sleep(2)

# 2. Start MQTT Communication Thread
_thread.start_new_thread(mqtt.mqtt_thread, (heartbeats,))

# 3. Run Watchdog
print("🛡️ System Monitor Active")
monitor_threads()