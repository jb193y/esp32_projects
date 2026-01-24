# mqtt.py
import time
import ujson
import network
import machine
import math
import _thread
import gc
from umqtt.simple import MQTTClient

import config
import gps
from ota import ota_update, fetch_manifest

# --- Global Control Flags (To prevent stack overflow) ---
_pending_ota_cmd = None

# -----------------------------
# Correction state (rover)
# -----------------------------
_correction_lock = None
_latest_correction = None
_latest_correction_recv_epoch = 0

def _ensure_corr_lock():
    global _correction_lock
    if _correction_lock is None:
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

# -----------------------------
# Math Helpers
# -----------------------------
def _meters_to_deg_lat(m):
    return m / 111320.0

def _meters_to_deg_lon(m, lat):
    c = math.cos(math.radians(lat))
    if c == 0: c = 1e-6
    return m / (111320.0 * c)

def _clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v

def haversine_m(lat1, lon1, lat2, lon2):
    R = 6371000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi, dlmb = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = (math.sin(dphi/2)**2) + math.cos(phi1) * math.cos(phi2) * (math.sin(dlmb/2)**2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

def ensure_network_ready():
    sta = network.WLAN(network.STA_IF)
    if not sta.isconnected(): return False
    if sta.ifconfig()[2] == "0.0.0.0": return False # Check for Gateway
    return True

# -----------------------------
# MQTT Helper Functions
# ----------------------------
def publish_status(client, status="",reason="OTA Update"):
    """Sends a final message before the hardware resets."""
    try:
        cfg = config.load_config()
        client_id = cfg.get("client", {}).get("id", "unknown")
        client_type = cfg.get("client", {}).get("type", "rover")
        topic = f"client/{client_id}/status"
        payload = ujson.dumps({
            "client_id": client_id,
            "client_type": client_type,
            "status": status,
            "reason": reason,
            "timestamp": time.time()
        })
        client.publish(topic, payload)
        print(f"📤 Reboot notification sent: {reason}")
        time.sleep(1) # Give the network a moment to flush the buffer
    except:
        pass # Don't block the reboot if MQTT fails

# -----------------------------
# Command Execution Logic
# -----------------------------
def handle_command(cmd):
    """Parses standard commands. Returns True if a reboot is needed."""
    print("📥 Command received:", cmd)
    command = cmd.get("command")
    cfg = config.load_config()
    changed = False

    if command == "REBOOT":
        machine.reset()
    elif command == "SET_MODE":
        cfg.setdefault("client", {})["mode"] = cmd.get("mode")
        changed = True
    elif command == "SET_CLIENT_TYPE":
        cfg.setdefault("client", {})["type"] = cmd.get("type")
        changed = True
    elif command == "SET_BASE":
        cfg.setdefault("base", {})["known_lat"] = float(cmd.get("known_lat"))
        cfg.setdefault("base", {})["known_lon"] = float(cmd.get("known_lon"))
        changed = True
    elif command == "SET_PUBLISH":
        cfg.setdefault("mqtt", {})["publish_every_sec"] = int(cmd.get("seconds"))
        changed = True

    if changed:
        config.save_config(cfg)
        print("💾 Config saved, rebooting...")
        time.sleep(1)
        machine.reset()

def run_ota_safely(client, cmd):
    gc.collect()
    cfg = config.load_config()
    ota_cfg = cfg.get("ota", {})
    base_url = ota_cfg.get("base_url")
    
    if not base_url:
        print("⚠️ OTA missing ota.base_url")
        return

    try:
        # 1. Inform dashboard that we are STARTING (Optional)
        # client.publish(topic, "OTA_START") 
        if client:
            publish_status(client, "updating", "OTA Update Starting")

        if cmd.get("manifest") is True:
            m_name = cmd.get("manifest_name") or ota_cfg.get("manifest", "manifest.json")
            print("📡 Fetching Manifest:", m_name)
            manifest = fetch_manifest(base_url, m_name)
            # Pass the client or handle reboot here
            success = ota_update(base_url, manifest=manifest)
        else:
            files = cmd.get("files", [])
            hashes = cmd.get("sha256", {})
            success = ota_update(base_url, files=files, hashes=hashes)

        # 2. SUCCESS! Now we notify and reboot
        if success and client:
            publish_status(client, "rebooting", "OTA Success - Rebooting")
            time.sleep(1) # Ensure MQTT packet leaves the buffer
            machine.reset()

    except Exception as e:
        print("❌ OTA Failed:", e)
        # Optional: Notify dashboard of failure

# -----------------------------
# MQTT Callbacks
# -----------------------------
def mqtt_callback(topic, msg):
    global _pending_ota_cmd
    try:
        t = topic.decode()
        payload = ujson.loads(msg.decode())
    except: return

    # Correction data (rover)
    if "/correction" in t or t.startswith("base/"):
        _set_correction(payload)
        return

    # OTA Command - Set flag for safety
    if payload.get("command") == "OTA":
        print("🚩 OTA Queued for execution...")
        _pending_ota_cmd = payload
    else:
        handle_command(payload)

# -----------------------------
# Main Thread Loop
# -----------------------------
def mqtt_thread(heartbeats=None):
    global _pending_ota_cmd
    
    # Increase stack size for this specific thread
    _thread.stack_size(8192)
    
    cfg = config.load_config()
    client_cfg = cfg.get("client", {})
    mqtt_cfg = cfg.get("mqtt", {})
    
    client_id = client_cfg.get("id", "esp32_gps")
    client_type = client_cfg.get("type", "rover")
    server = mqtt_cfg.get("server", "10.10.10.211")
    port = int(mqtt_cfg.get("port", 1883))
    
    pub_topic = f"client/{client_id}/location"
    cmd_topic = f"client/{client_id}/command"
    base_corr_topic = f"client/{client_id}/correction"
    nmea_topic = f"client/{client_id}/nmea"
    
    rover_base_id = client_cfg.get("base_id")
    rover_corr_topic = f"client/{rover_base_id}/correction" if rover_base_id else None
    
    PUBLISH_EVERY_SEC = int(mqtt_cfg.get("publish_every_sec", 10))
    NMEA_PUBLISH_EVERY_SEC = int(mqtt_cfg.get("nmea_publish_every_sec", 5))
    PUBLISH_EVERY_SEC_NO_CORR = int(mqtt_cfg.get("publish_every_sec_no_corr", 30))  # Stricter interval without corrections
    MOVE_THRESHOLD_M = float(mqtt_cfg.get("move_threshold_m", 5.0))
    CORR_TIMEOUT_S = int(mqtt_cfg.get("correction_timeout_s", 5))
    CORR_MAX_M = float(mqtt_cfg.get("correction_max_m", 5.0))

    last_pub_time = 0
    last_nmea_pub_time = 0
    last_pub_lat, last_pub_lon = None, None
    last_corrected = False

    while True:
        if not ensure_network_ready():
            time.sleep(2)
            continue

        client = MQTTClient(client_id, server, port)
        client.set_callback(mqtt_callback)
        
        try:
            client.connect()
            client.subscribe(cmd_topic)
            if client_type == "rover" and rover_corr_topic:
                client.subscribe(rover_corr_topic)
            
            print("✅ MQTT Connected to %s" % server)

            if client:
                publish_status(client, "online", "System is online")

            while True:
                # Update Watchdog Heartbeat
                if heartbeats: heartbeats["mqtt"] = time.time()
                
                # Check for incoming messages
                client.check_msg()

                # Execute OTA if flag was set in callback
                if _pending_ota_cmd:
                    run_ota_safely(client, _pending_ota_cmd)
                    _pending_ota_cmd = None

                now = time.time()
                
                # Publish NMEA/raw GPS data every N seconds
                if now - last_nmea_pub_time >= NMEA_PUBLISH_EVERY_SEC:
                    gps.lock.acquire()
                    try:
                        raw_data = {
                            "timestamp": gps.gps_raw_data["timestamp"],
                            "nmea_sentences": list(gps.gps_raw_data["nmea_sentences"])
                        }
                    finally:
                        gps.lock.release()
                    
                    if raw_data.get("nmea_sentences"):
                        nmea_payload = {
                            "client_id": client_id,
                            "client_type": client_type,
                            "timestamp": raw_data.get("timestamp"),
                            "nmea_sentences": raw_data.get("nmea_sentences", [])
                        }
                        client.publish(nmea_topic, ujson.dumps(nmea_payload))
                        last_nmea_pub_time = now
                
                # Fetch current GPS data
                gps.lock.acquire()
                try:
                    data = dict(gps.gps_data)
                finally:
                    gps.lock.release()

                lat, lon, ts = data.get("lat"), data.get("lon"), data.get("timestamp")

                if lat is None or lon is None:
                    time.sleep(0.5)
                    continue

                # --- BASE STATION LOGIC ---
                if client_type == "base":
                    known_lat = cfg.get("base", {}).get("known_lat")
                    known_lon = cfg.get("base", {}).get("known_lon")
                    if known_lat is not None and (now - last_pub_time >= 1):
                        corr_payload = {
                            "client_id": client_id,
                            "client_type": client_type,
                            "timestamp": ts,
                            "delta_lat": known_lat - lat, "delta_lon": known_lon - lon,
                            "hdop": data.get("hdop")
                        }
                        client.publish(base_corr_topic, ujson.dumps(corr_payload))
                        last_pub_time = now

                # --- ROVER LOGIC ---
                else:
                    corr, corr_recv = _get_correction()
                    has_valid_corr = corr and (now - corr_recv <= CORR_TIMEOUT_S)
                    
                    # Use stricter publish interval if no valid corrections
                    pub_interval = PUBLISH_EVERY_SEC if has_valid_corr else PUBLISH_EVERY_SEC_NO_CORR
                    
                    should_pub = (now - last_pub_time >= pub_interval)
                    if not should_pub and last_pub_lat:
                        if haversine_m(last_pub_lat, last_pub_lon, lat, lon) >= MOVE_THRESHOLD_M:
                            should_pub = True

                    if should_pub:
                        corrected = False
                        # Apply DGPS correction if fresh enough
                        if has_valid_corr:
                            dlat = _clamp(float(corr.get("delta_lat", 0)), -_meters_to_deg_lat(CORR_MAX_M), _meters_to_deg_lat(CORR_MAX_M))
                            dlon = _clamp(float(corr.get("delta_lon", 0)), -_meters_to_deg_lon(CORR_MAX_M, lat), _meters_to_deg_lon(CORR_MAX_M, lat))
                            lat += dlat
                            lon += dlon
                            corrected = True

                        payload = {
                            "client_id": client_id,
                            "client_type": client_type,
                            "timestamp": ts, "latitude": lat, "longitude": lon,
                            "hdop": data.get("hdop"), "sats": data.get("sats"), "locked": data.get("locked"),
                            "corrected": corrected
                        }
                        client.publish(pub_topic, ujson.dumps(payload))
                        last_pub_time, last_pub_lat, last_pub_lon = now, lat, lon

                time.sleep(0.1)

        except Exception as e:
            print("⚠️ MQTT Error:", e)
            try: client.disconnect()
            except: pass
            time.sleep(5)
