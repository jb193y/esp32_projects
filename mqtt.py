# mqtt.py
import time
import ujson
import network
import machine
import math
from umqtt.simple import MQTTClient

import config
import gps
from ota import ota_update, fetch_manifest


# -----------------------------
# Correction state (rover)
# -----------------------------
_correction_lock = None
_latest_correction = None
_latest_correction_recv_epoch = 0

def _ensure_corr_lock():
    global _correction_lock
    if _correction_lock is None:
        import _thread
        _correction_lock = _thread.allocate_lock()

def _set_correction(corr):
    global _latest_correction, _latest_correction_recv_epoch
    _ensure_corr_lock()
    _correction_lock.acquire()
    try:
        _latest_correction = corr
        _latest_correction_recv_epoch = time.time()
    finally:
        _correction_lock.release()

def _get_correction():
    _ensure_corr_lock()
    _correction_lock.acquire()
    try:
        return _latest_correction, _latest_correction_recv_epoch
    finally:
        _correction_lock.release()

def _meters_to_deg_lat(m):
    return m / 111320.0

def _meters_to_deg_lon(m, lat):
    # avoid division by zero near poles; good enough for your use
    c = math.cos(math.radians(lat))
    if c == 0:
        c = 1e-6
    return m / (111320.0 * c)

def _clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


# -----------------------------
def haversine_m(lat1, lon1, lat2, lon2):
    R = 6371000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = (math.sin(dphi/2)**2) + math.cos(phi1) * math.cos(phi2) * (math.sin(dlmb/2)**2)
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
    return R * c


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


# -----------------------------
# Base correction helpers
# -----------------------------
def compute_correction(measured_lat, measured_lon, known_lat, known_lon):
    return {
        "delta_lat": known_lat - measured_lat,
        "delta_lon": known_lon - measured_lon
    }

def validate_base_config(cfg):
    device = cfg.get("device", {})
    if device.get("type") != "base":
        return True
    base_cfg = cfg.get("base", {})
    return (base_cfg.get("known_lat") is not None and base_cfg.get("known_lon") is not None)


# -----------------------------
# MQTT callback (commands + rover correction)
# -----------------------------
def mqtt_callback(topic, msg):
    try:
        t = topic.decode() if isinstance(topic, (bytes, bytearray)) else str(topic)
    except:
        t = str(topic)

    try:
        s = msg.decode() if isinstance(msg, (bytes, bytearray)) else str(msg)
    except:
        s = str(msg)

    # Correction topic (rover)
    if "/correction" in t or t.startswith("base/"):
        try:
            payload = ujson.loads(s)
            _set_correction(payload)
            print("🧭 Correction received:", payload)
            return
        except Exception as e:
            print("⚠️ Bad correction payload:", e)
            return

    # Command topic
    try:
        payload = ujson.loads(s)
        print("📥 CMD:", payload)
        handle_command(payload)
    except Exception as e:
        print("⚠️ Bad CMD payload:", e)


# -----------------------------
def mqtt_thread():
    cfg = config.load_config()

    device = cfg.get("device", {})
    mqtt_cfg = cfg.get("mqtt", {})
    base_cfg = cfg.get("base", {})

    client_id = device.get("id", "esp32_gps")
    device_type = device.get("type", "rover")  # rover/base

    server = mqtt_cfg.get("server", "10.10.10.211")
    port = int(mqtt_cfg.get("port", 1883))

    PUBLISH_EVERY_SEC = int(mqtt_cfg.get("publish_every_sec", 10))
    MOVE_THRESHOLD_M = float(mqtt_cfg.get("move_threshold_m", 5.0))
    MAX_RETRIES = int(mqtt_cfg.get("max_retries", 5))
    RETRY_DELAY = int(mqtt_cfg.get("retry_delay", 5))

    # Correction tuning (optional config keys)
    CORR_TIMEOUT_S = int(mqtt_cfg.get("correction_timeout_s", 5))
    CORR_MAX_M = float(mqtt_cfg.get("correction_max_m", 5.0))

    pub_topic = "device/%s/location" % client_id
    cmd_topic = "device/%s/command" % client_id

    # Base correction topic
    base_corr_topic = "base/%s/correction" % client_id

    # Rover subscribes to selected base
    rover_base_id = device.get("base_id")  # for rover
    rover_corr_topic = None
    if rover_base_id:
        rover_corr_topic = "base/%s/correction" % rover_base_id

    last_pub_time = 0
    last_pub_lat = None
    last_pub_lon = None
    last_save = time.time()

    # Validation for base mode
    if device_type == "base" and not validate_base_config(cfg):
        print("❌ BASE mode requires base.known_lat and base.known_lon")
        # still run; base will just not publish corrections

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

                # Always subscribe to commands
                client.subscribe(cmd_topic)

                # Rover subscribes to corrections
                if device_type == "rover" and rover_corr_topic:
                    client.subscribe(rover_corr_topic)
                    print("🛰 Subscribed correction:", rover_corr_topic)

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
                # receive messages
                try:
                    client.check_msg()
                except Exception:
                    pass

                now = time.time()
                if now - last_save > 1800:
                    last_save = now
                    cfg = config.load_config()
                    config.save_time(cfg)

                # snapshot GPS
                gps.lock.acquire()
                try:
                    data = dict(gps.gps_data)
                finally:
                    gps.lock.release()

                lat = data.get("lat")
                lon = data.get("lon")
                ts = data.get("timestamp")

                # -----------------------------
                # BASE: publish corrections
                # -----------------------------
                if device_type == "base":
                    known_lat = base_cfg.get("known_lat")
                    known_lon = base_cfg.get("known_lon")

                    if lat is not None and lon is not None and ts and known_lat is not None and known_lon is not None:
                        corr = compute_correction(lat, lon, known_lat, known_lon)
                        corr_payload = {
                            "base_id": client_id,
                            "timestamp": ts,
                            "delta_lat": corr["delta_lat"],
                            "delta_lon": corr["delta_lon"],
                            "hdop": data.get("hdop"),
                            "confidence_m": data.get("confidence_m")
                        }
                        # publish at low rate (1 Hz)
                        if (now - last_pub_time) >= 1:
                            client.publish(base_corr_topic, ujson.dumps(corr_payload))
                            print("📡 Correction published:", corr_payload)
                            last_pub_time = now

                    time.sleep(0.2)
                    continue  # base does not publish location unless you want it to

                # -----------------------------
                # ROVER: publish location (with correction applied)
                # -----------------------------
                should_publish = False
                if lat is not None and lon is not None and ts is not None:
                    # movement trigger
                    if last_pub_lat is not None and last_pub_lon is not None:
                        try:
                            d = haversine_m(last_pub_lat, last_pub_lon, lat, lon)
                            if d >= MOVE_THRESHOLD_M:
                                should_publish = True
                        except:
                            pass
                    else:
                        should_publish = True

                    # heartbeat
                    if (now - last_pub_time) >= PUBLISH_EVERY_SEC:
                        should_publish = True

                    if should_publish:
                        corrected = False
                        corr_used = None
                        corr_age_s = None

                        corr, corr_recv = _get_correction()
                        if corr:
                            corr_age_s = now - corr_recv
                            if corr_age_s <= CORR_TIMEOUT_S:
                                try:
                                    dlat = float(corr.get("delta_lat", 0.0))
                                    dlon = float(corr.get("delta_lon", 0.0))

                                    # bound correction magnitude in meters
                                    max_lat = _meters_to_deg_lat(CORR_MAX_M)
                                    max_lon = _meters_to_deg_lon(CORR_MAX_M, lat)

                                    dlat = _clamp(dlat, -max_lat, max_lat)
                                    dlon = _clamp(dlon, -max_lon, max_lon)

                                    lat_corr = lat + dlat
                                    lon_corr = lon + dlon

                                    # Use corrected values
                                    lat, lon = lat_corr, lon_corr
                                    corrected = True
                                    corr_used = {
                                        "base_id": corr.get("base_id"),
                                        "age_s": corr_age_s
                                    }
                                except:
                                    pass

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
                            "corrected": corrected,
                            "correction": corr_used
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

    device = cfg.get("device", {})
    mqtt_cfg = cfg.get("mqtt", {})
    gps_cfg = cfg.get("gps", {})
    ota_cfg = cfg.get("ota", {})

    if command == "REBOOT":
        print("🔁 Rebooting device...")
        time.sleep(1)
        machine.reset()

    elif command == "SET_MODE":
        mode = cmd.get("mode")
        if mode in ("ap", "sta"):
            device["mode"] = mode
            cfg["device"] = device
            changed = True
            print("📡 Mode set to", mode)

    elif command == "SET_DEVICE_TYPE":
        # allow switching rover/base
        dtype = cmd.get("type")
        if dtype in ("rover", "base"):
            device["type"] = dtype
            cfg["device"] = device
            changed = True
            print("🧭 Device type set to", dtype)

    elif command == "SET_BASE":
        # base-only coords
        known_lat = cmd.get("known_lat")
        known_lon = cmd.get("known_lon")
        if device.get("type") == "base" and isinstance(known_lat, (int, float)) and isinstance(known_lon, (int, float)):
            cfg.setdefault("base", {})["known_lat"] = float(known_lat)
            cfg.setdefault("base", {})["known_lon"] = float(known_lon)
            changed = True
            print("📍 Base coords updated")

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

    elif command == "SET_TIME":
        if "epoch" in cmd:
            config.set_time_from_epoch(cfg, cmd["epoch"])
        elif "timestamp" in cmd:
            config.set_time_from_iso(cfg, cmd["timestamp"])

    elif command == "OTA":
        base_url = ota_cfg.get("base_url")
        if not base_url:
            print("⚠️ OTA missing ota.base_url in config")
            return

        try:
            if cmd.get("manifest") is True:
                manifest_name = cmd.get("manifest_name") or ota_cfg.get("manifest", "manifest.json")
                manifest = fetch_manifest(base_url, manifest_name)
                ota_update(base_url, manifest=manifest)
            else:
                files = cmd.get("files", [])
                hashes = cmd.get("sha256")
                ota_update(base_url, files=files, hashes=hashes)
        except Exception as e:
            print("❌ OTA failed:", e)
            return

    else:
        print("⚠️ Unknown command")

    if changed:
        # Validation: base.* only if base
        if cfg.get("device", {}).get("type") != "base" and "base" in cfg:
            # keep base section but it is ignored; no destructive delete here
            pass

        config.save_config(cfg)
        print("💾 Config saved, rebooting to apply changes...")
        time.sleep(1)
        machine.reset()
