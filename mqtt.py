# mqtt.py
import time
import ujson
import network
import usocket as socket
import machine
from umqtt.simple import MQTTClient
import config
import gps
from ota import ota_update, fetch_manifest

def haversine_m(lat1, lon1, lat2, lon2):
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
    try:
        payload = ujson.loads(msg)
        print("📥 CMD:", payload)
        handle_command(payload)
    except Exception as e:
        print("⚠️ Bad CMD payload:", e)

def ensure_network_ready():
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

    dev = cfg.get("device", {})
    mqtt_cfg = cfg.get("mqtt", {})

    client_id = dev.get("client_id", "esp32_gps")
    server = mqtt_cfg.get("server", "10.10.10.211")
    port = int(mqtt_cfg.get("port", 1883))

    PUBLISH_EVERY_SEC = int(mqtt_cfg.get("publish_every_sec", 10))
    MOVE_THRESHOLD_M = float(mqtt_cfg.get("move_threshold_m", 5.0))
    MAX_RETRIES = int(mqtt_cfg.get("max_retries", 5))
    RETRY_DELAY = int(mqtt_cfg.get("retry_delay", 5))

    pub_topic = "device/%s/location" % client_id
    cmd_topic = "device/%s/command" % client_id

    last_pub_time = 0
    last_pub_lat = None
    last_pub_lon = None
    last_save = time.time()

    while True:
        if not ensure_network_ready():
            print("⏳ Network not ready, retrying...")
            time.sleep(2)
            continue

        client = MQTTClient(client_id, server, port)
        client.set_callback(mqtt_callback)

        connected = False
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                print("🔄 MQTT connecting... %s to %s:%d" % (client_id, server, port))
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

        try:
            while True:
                # receive commands
                try:
                    client.check_msg()
                except Exception:
                    pass

                # time persistence
                now = time.time()
                if now - last_save > 1800:
                    last_save = now
                    cfg = config.load_config()
                    config.save_time(cfg)

                # get GPS snapshot
                gps.lock.acquire()
                try:
                    data = dict(gps.gps_data)
                finally:
                    gps.lock.release()

                lat = data.get("lat")
                lon = data.get("lon")
                ts = data.get("timestamp")

                should_publish = False
                if lat is not None and lon is not None and ts is not None:
                    if last_pub_lat is not None and last_pub_lon is not None:
                        try:
                            d = haversine_m(last_pub_lat, last_pub_lon, lat, lon)
                            if d >= MOVE_THRESHOLD_M:
                                should_publish = True
                        except:
                            pass
                    else:
                        should_publish = True

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

    dev = cfg.get("device", {})
    mqtt_cfg = cfg.get("mqtt", {})
    gps_cfg = cfg.get("gps", {})
    ota_cfg = cfg.get("ota", {})
    time_cfg = cfg.get("time", {})

    if command == "REBOOT":
        print("🔁 Rebooting device...")
        time.sleep(1)
        machine.reset()

    elif command == "SET_MODE":
        mode = cmd.get("mode")
        if mode in ("ap", "sta"):
            dev["mode"] = mode
            cfg["device"] = dev
            changed = True
            print("📡 Mode set to", mode)

    elif command == "SET_THRESH":
        move_m = cmd.get("move_m")
        if isinstance(move_m, (int, float)) and move_m > 0:
            mqtt_cfg["move_threshold_m"] = float(move_m)
            cfg["mqtt"] = mqtt_cfg
            changed = True
            print("📐 Move threshold set to", move_m, "m")

    elif command == "SET_HDOP":
        hdop = cmd.get("max")
        if isinstance(hdop, (int, float)) and hdop > 0:
            gps_cfg["hdop_max"] = float(hdop)
            cfg["gps"] = gps_cfg
            changed = True
            print("📡 HDOP max set to", hdop)

    elif command == "SET_PUBLISH":
        interval = cmd.get("seconds")
        if isinstance(interval, (int, float)) and interval >= 1:
            mqtt_cfg["publish_every_sec"] = int(interval)
            cfg["mqtt"] = mqtt_cfg
            changed = True
            print("⏱ Publish interval set to", interval, "sec")

    elif command == "STATUS":
        print("📊 Current config:", cfg)

    elif command == "SET_TIME":
        if "epoch" in cmd:
            config.set_time_from_epoch(cfg, cmd["epoch"])
        elif "timestamp" in cmd:
            config.set_time_from_iso(cfg, cmd["timestamp"])

    elif command == "OTA":
        base_url = ota_cfg.get("base_url")
        if not base_url:
            print("⚠️ OTA missing base_url in config")
            return

        try:
            # Mode A: fetch manifest from server
            if cmd.get("manifest") is True:
                manifest_name = cmd.get("manifest_name") or ota_cfg.get("manifest", "manifest.json")
                manifest = fetch_manifest(base_url, manifest_name)
                ota_update(base_url, manifest=manifest)

            # Mode B: explicit file list and hashes
            else:
                files = cmd.get("files", [])
                hashes = cmd.get("sha256")  # dict
                ota_update(base_url, files=files, hashes=hashes)

        except Exception as e:
            print("❌ OTA failed:", e)
            return

    else:
        print("⚠️ Unknown command")

    if changed:
        config.save_config(cfg)
        print("💾 Config saved, rebooting to apply changes...")
        time.sleep(1)
        machine.reset()
