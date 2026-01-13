import time
import ujson
import network
import usocket as socket
from umqtt.simple import MQTTClient
import config
import gps
from ota import ota_update

# -----------------------------
# CONFIG (tune)
# -----------------------------
# Load from config
cfg = config.load_config()
PUBLISH_EVERY_SEC = cfg.get("publish_every_sec", 10)    # publish heartbeat at least this often
MOVE_THRESHOLD_M = cfg.get("move_threshold_m", 5.0)     # publish immediately if moved more than this

# MQTT connection retries
MAX_RETRIES = cfg.get("mqtt_max_retries", 5)            # max connection attempts
RETRY_DELAY = cfg.get("mqtt_retry_delay", 5)            # seconds between attempts

# -----------------------------
def haversine_m(lat1, lon1, lat2, lon2):
    # small duplicate helper to avoid import cycles / extra deps
    import math
    R = 6371000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = (math.sin(dphi/2)**2) + math.cos(phi1) * math.cos(phi2) * (math.sin(dlmb/2)**2)
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
    return R * c

def mqtt_callback(topic, msg):
    # Add your command handling here
    try:
        payload = ujson.loads(msg)
        print("📥 CMD:", payload)
        handle_command(payload)
    except Exception as e:
        print("⚠️ Bad CMD payload:", e)

def ensure_network_ready():
    # force AP off (ESP32 stability)
    ap = network.WLAN(network.AP_IF)
    if ap.active():
        ap.active(False)
        time.sleep(1)

    sta = network.WLAN(network.STA_IF)
    if not sta.isconnected():
        return False

    ip, mask, gw, dns = sta.ifconfig()
    if gw == "0.0.0.0":
        return False
    return True

def mqtt_thread():
    cfg = config.load_config()
    client_id = cfg.get("client_id", "esp32_gps")
    server = cfg.get("mqtt_server", "10.10.10.211")
    port = int(cfg.get("mqtt_port", 1883))

    pub_topic = "device/%s/location" % client_id
    cmd_topic = "device/%s/command" % client_id

    last_pub_time = 0
    last_pub_lat = None
    last_pub_lon = None
    last_save = time.time()

    while True:
        # Wait for Wi-Fi routing
        if not ensure_network_ready():
            print("⏳ Network not ready, retrying...")
            time.sleep(2)
            continue

        client = MQTTClient(client_id, server, port)
        client.set_callback(mqtt_callback)   # MUST be before subscribe

        connected = False
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                print("🔄 MQTT connecting... %s to %s:%d" % (client_id, server, port))
                # let routing settle
                time.sleep(2)
                client.connect()
                client.subscribe(cmd_topic)
                print("✅ MQTT connected")
                print("📡 Subscribed:", cmd_topic)
                connected = True
                break
            except Exception as e:
                print("MQTT retry failed:", e)
                time.sleep(RETRY_DELAY)

        if not connected:
            print("🚫 MQTT failed after retries. Will retry in 10s.")
            time.sleep(10)
            continue

        # Main MQTT loop (check commands + publish when needed)
        try:
            while True:
                # receive commands (non-blocking)
                try:
                    client.check_msg()
                except Exception:
                    pass

                # get current GPS
                gps.lock.acquire()
                try:
                    data = dict(gps.gps_data)
                finally:
                    gps.lock.release()

                lat = data.get("lat")
                lon = data.get("lon")
                ts = data.get("timestamp")

                now = time.time()
                if now - last_save > 1800:  # every 30 min
                    last_save = now
                    config.save_time(cfg)
                should_publish = False

                if lat is not None and lon is not None and ts is not None:
                    # publish on movement
                    if last_pub_lat is not None and last_pub_lon is not None:
                        try:
                            d = haversine_m(last_pub_lat, last_pub_lon, lat, lon)
                            if d >= MOVE_THRESHOLD_M:
                                should_publish = True
                        except:
                            pass
                    else:
                        should_publish = True  # first fix

                    # publish heartbeat
                    if (now - last_pub_time) >= PUBLISH_EVERY_SEC:
                        should_publish = True

                    if should_publish:
                        payload = {
                            "client_id": client_id,
                            "timestamp": ts,
                            "latitude": lat,
                            "longitude": lon,
                            "hdop": data.get("hdop"),
                            "sats": data.get("sats"),
                            "speed_kmh": data.get("speed_kmh"),
                            "confidence_m": data.get("confidence_m"),
                            "locked": data.get("locked"),
                        }
                        client.publish(pub_topic, ujson.dumps(payload))
                        print("📤 Published:", payload)

                        last_pub_time = now
                        last_pub_lat, last_pub_lon = lat, lon

                time.sleep(0.2)
        except Exception as e:
            print("⚠️ MQTT loop error, will reconnect:", e)
            try:
                client.disconnect()
            except:
                pass
            time.sleep(2)

# -------------------------------------------------
# COMMAND HANDLER
# -------------------------------------------------
def handle_command(cmd):
    print("📥 Command received:", cmd)

    command = cmd.get("command")
    cfg = config.load_config()
    changed = False

    # -------------------------
    # SYSTEM COMMANDS
    # -------------------------
    if command == "REBOOT":
        print("🔁 Rebooting device...")
        time.sleep(1)
        machine.reset()

    elif command == "SET_MODE":
        mode = cmd.get("mode")
        if mode in ("ap", "sta"):
            cfg["mode"] = mode
            changed = True
            print(f"📡 Mode set to {mode}")

    # -------------------------
    # GPS / FILTER TUNING
    # -------------------------
    elif command == "SET_THRESH":
        # meters
        move_m = cmd.get("move_m")
        if isinstance(move_m, (int, float)) and move_m > 0:
            cfg["move_threshold_m"] = float(move_m)
            changed = True
            print(f"📐 Move threshold set to {move_m} m")

    elif command == "SET_HDOP":
        hdop = cmd.get("max")
        if isinstance(hdop, (int, float)) and hdop > 0:
            cfg["hdop_max"] = float(hdop)
            changed = True
            print(f"📡 HDOP max set to {hdop}")

    elif command == "SET_PUBLISH":
        # seconds
        interval = cmd.get("seconds")
        if isinstance(interval, (int, float)) and interval >= 1:
            cfg["publish_every_sec"] = int(interval)
            changed = True
            print(f"⏱ Publish interval set to {interval} sec")

    # -------------------------
    # DIAGNOSTICS
    # -------------------------
    elif command == "STATUS":
        print("📊 Current config:", cfg)
        # (Optional) publish status back via MQTT
        # mqtt_client.publish(status_topic, ujson.dumps(cfg))

    # -------------------------
    # TIME COMMANDS
    # -------------------------    
    elif cmd.get("command") == "SET_TIME":
        if "epoch" in cmd:
            config.set_time_from_epoch(cfg, cmd["epoch"])
        elif "timestamp" in cmd:
            config.set_time_from_iso(cfg, cmd["timestamp"])
    
    elif cmd.get("command") == "OTA":
        files = cmd.get("files", [])
        hashes = cmd.get("sha256")
        ota_update(cfg.ota["base_url"], files, hashes)

    else:
        print("⚠️ Unknown command")

    # -------------------------
    # SAVE + REBOOT IF NEEDED
    # -------------------------
    if changed:
        config.save_config(cfg)
        print("💾 Config saved, rebooting to apply changes...")
        time.sleep(1)
        machine.reset()
