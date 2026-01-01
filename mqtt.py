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
    print("🚀 MQTT thread started")
    print("⏳ Reading config...")
    cfg = config.load_config()

    print("🔌 WiFi check...")
    # 🔑 CRITICAL FIX
    disable_ap()
    
    if not wait_for_wifi():
        print("❌ WiFi never came up, aborting MQTT")
        return
    time.sleep(3)  # Allow network to stabilize

    client_id = cfg["client_id"]
    server = cfg["mqtt_server"]
    port = int(cfg.get("mqtt_port", 1883))

    raw_topic = f"device/{client_id}/raw"
    pub_topic = f"device/{client_id}/location"
    cmd_topic = f"device/{client_id}/command"

    print("🔄 Connecting to MQTT broker...")
    MAX_RETRIES = 5
    RETRY_DELAY = 3  # seconds

    connected = False

    while True:
        client = MQTTClient(client_id, server, port)
        client.set_callback(mqtt_callback)
        connected = False

        for attempt in range(1, MAX_RETRIES + 1):
            print(f"🔄 MQTT connect attempt {attempt}...")
            print(f"🔄 MQTT connecting... {client_id} to {server}:{port}")
            try:
                client.connect()
                client.subscribe(cmd_topic)
                connected = True
                break
            except Exception as e:
                print("MQTT retry failed:", e)
                time.sleep(RETRY_DELAY)

        if not connected:
            print("Retrying MQTT in {RETRY_DELAY}s...")
            time.sleep(RETRY_DELAY)
            continue
        else:
            break


    print("✅ MQTT connected and subscribed to command topic")
    last_publish = 0

    while True:
        try:
            # 🔹 Check for incoming commands (NON-BLOCKING)
            client.check_msg()

            # 🔹 Publish GPS periodically
            if time.time() - last_publish >= PUBLISH_INTERVAL:
                with gps.lock:
                    data = gps.gps_data.copy()
                print(data)
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

    if cmd.get("command") == "REBOOT":
        print("🔁 Rebooting device...")
        time.sleep(1)
        machine.reset()

    elif cmd.get("command") == "SET_MODE":
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




