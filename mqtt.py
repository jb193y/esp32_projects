import _thread
import network
import time
import ujson
from umqtt.simple import MQTTClient
import config
import gps
import machine

# ------------------------------------------------------------------
# GLOBALS
# ------------------------------------------------------------------
lock = _thread.allocate_lock()
PUBLISH_INTERVAL = 10  # seconds

def disable_ap():
    ap = network.WLAN(network.AP_IF)
    if ap.active():
        print("📴 Disabling AP mode")
        ap.active(False)

def wait_for_wifi(timeout=20):
    sta = network.WLAN(network.STA_IF)
    for i in range(timeout):
        if sta.isconnected():
            print("📶 WiFi ready:", sta.ifconfig())
            return True
        print("⏳ Waiting for WiFi...")
        time.sleep(1)
    return False

# -------------------------------------------------
# MQTT CALLBACK
# -------------------------------------------------
def mqtt_callback(topic, msg):
    try:
        payload = ujson.loads(msg)
        handle_command(payload)
    except Exception as e:
        print("❌ Invalid MQTT payload:", e)

# -------------------------------------------------
# MQTT THREAD
# -------------------------------------------------
def mqtt_thread():
    cfg = config.load_config()

    # 🔑 CRITICAL FIX
    disable_ap()
    
    if not wait_for_wifi():
        print("❌ WiFi never came up, aborting MQTT")
        return
    time.sleep(3)  # Allow network to stabilize

    client_id = cfg["device_id"]
    server = cfg["mqtt_server"]
    port = int(cfg.get("mqtt_port", 1883))

    pub_topic = f"device/{client_id}/location"
    cmd_topic = f"device/{client_id}/command"

    client = MQTTClient(client_id, server, port)
    client.set_callback(mqtt_callback)

    try:
        client.connect()
        client.subscribe(cmd_topic)
        print("✅ MQTT connected")
        print("📡 Subscribed to:", cmd_topic)
    except Exception as e:
        print("❌ MQTT connection failed:", e)
        return

    last_publish = 0

    while True:
        try:
            # 🔹 Check for incoming commands (NON-BLOCKING)
            client.check_msg()

            # 🔹 Publish GPS periodically
            if time.time() - last_publish >= PUBLISH_INTERVAL:
                with gps.lock:
                    data = gps.gps_data.copy()

                if data["lat"] and data["lon"]:
                    payload = {
                        "device_id": client_id,
                        "timestamp": data["timestamp"],
                        "latitude": data["lat"],
                        "longitude": data["lon"]
                    }
                    client.publish(pub_topic, ujson.dumps(payload))
                    print("📤 Location published")

                last_publish = time.time()

        except Exception as e:
            print("⚠️ MQTT loop error:", e)
            time.sleep(3)

        time.sleep(0.2)  # VERY IMPORTANT – do not remove

# -------------------------------------------------
# COMMAND HANDLER
# -------------------------------------------------
def handle_command(cmd):
    print("📥 Command received:", cmd)

    if cmd.get("command") == "reboot":
        print("🔁 Rebooting device...")
        time.sleep(1)
        machine.reset()

    elif cmd.get("command") == "set_mode":
        mode = cmd.get("mode")
        if mode in ("ap", "sta"):
            cfg = config.load_config()
            cfg["mode"] = mode
            config.save_config(cfg)
            print(f"📡 Mode set to {mode}, rebooting...")
            time.sleep(1)
            machine.reset()

    else:
        print("⚠️ Unknown command")




