# mqtt.py
import time
import ujson
import network
import machine
import _thread
import gc
from umqtt.simple import MQTTClient

import config
import led_status
import pump_controller
import gps
from ota import ota_update, fetch_manifest

# --- Global Control Flags ---
_pending_ota_cmd = None

def ensure_network_ready():
    sta = network.WLAN(network.STA_IF)
    if not sta.isconnected(): return False
    if sta.ifconfig()[2] == "0.0.0.0": return False # Check for Gateway
    return True

def publish_status(client, status="", reason="Running"):
    """Sends a connection status message."""
    try:
        cfg = config.load_config()
        client_id = cfg.get("client", {}).get("id", "esp32_pump_01")
        topic = f"pump/{client_id}/status"
        payload = ujson.dumps({
            "client_id": client_id,
            "status": status,
            "reason": reason,
            "timestamp": time.time()
        })
        client.publish(topic, payload)
        time.sleep(0.5)
    except:
        pass

def publish_version_announcement(client):
    """Announces online status, IP address, and firmware version to the ecosystem broker."""
    try:
        cfg = config.load_config()
        client_cfg = cfg.get("client", {})
        client_id = client_cfg.get("id", "esp32_pump_01")
        device_type = client_cfg.get("type", "pump")
        hw_ver = client_cfg.get("hardware_version", "esp32_1.0")
        fw_ver = client_cfg.get("firmware_version", "firmesp32_v2")
        
        # Get IP address
        sta = network.WLAN(network.STA_IF)
        ip_addr = sta.ifconfig()[0] if sta.isconnected() else "0.0.0.0"
        
        topic = f"{device_type}/{hw_ver}/status"
        payload = ujson.dumps({
            "client_id": client_id,
            "status": "online",
            "firmware_version": fw_ver,
            "ip": ip_addr,
            "timestamp": time.time()
        })
        client.publish(topic, payload)
        print(f"📡 MQTT OTA VERSION ANNOUNCED on {topic}: {payload}")
    except Exception as e:
        print("⚠️ Failed to publish version announcement:", e)

def publish_alert(client, event_type, message):
    """Sends an immediate alert message."""
    try:
        cfg = config.load_config()
        client_id = cfg.get("client", {}).get("id", "esp32_pump_01")
        topic = f"pump/{client_id}/alerts"
        
        pump_controller.lock.acquire()
        try:
            tele = dict(pump_controller.telemetry)
            faults = list(pump_controller.active_faults)
        finally:
            pump_controller.lock.release()
            
        payload = ujson.dumps({
            "client_id": client_id,
            "timestamp": time.time(),
            "event": event_type,
            "message": message,
            "faults": faults,
            "voltages": [tele.get("v_a"), tele.get("v_b"), tele.get("v_c")],
            "currents": [tele.get("i_a"), tele.get("i_b"), tele.get("i_c")]
        })
        client.publish(topic, payload)
        print("📢 MQTT ALERT SENT:", event_type, "-", message)
    except Exception as e:
        print("🚨 Failed to publish alert:", e)

def handle_command(payload):
    """Parses standard commands."""
    print("📥 Command processing:", payload)
    command = payload.get("command")
    val = payload.get("val")
    cfg = config.load_config()
    changed = False

    if command == "REBOOT":
        machine.reset()
        
    elif command in ["PUMP_ON", "PUMP_OFF", "SET_MODE", "CLEAR_FAULT", 
                     "SIM_VOLTAGE", "SIM_CURRENT", "SIM_ESTOP", "SIM_FLOW", "SIM_TANK"]:
        # Relay directly to pump controller
        pump_controller.pump_command(command, val)
        
    elif command == "SET_PUBLISH":
        cfg.setdefault("mqtt", {})["publish_every_sec"] = int(val)
        changed = True

    if changed:
        config.save_config(cfg)
        print("💾 Config saved, rebooting to apply changes...")
        time.sleep(1)
        machine.reset()

def run_ota_safely(client, cmd):
    gc.collect()
    cfg = config.load_config()
    client_cfg = cfg.get("client", {})
    ota_cfg = cfg.get("ota", {})
    base_url = ota_cfg.get("base_url")
    
    if not base_url:
        print("⚠️ OTA missing ota.base_url")
        return

    # Check firmware version from incoming OTA command
    target_version = cmd.get("version")
    current_version = client_cfg.get("firmware_version", "firmesp32_v2")
    if target_version and target_version == current_version:
        print(f"ℹ️ Firmware is already up to date: {current_version} (skipped OTA download)")
        try:
            import display_manager
            display_manager.show_command_toast("OTA (Up to date)")
        except:
            pass
        if client:
            publish_status(client, "online", f"Already on version {current_version}")
        return

    device_type = client_cfg.get("type", "pump")
    hw_ver = client_cfg.get("hardware_version", "esp32_1.0")
    if target_version:
        release_url = f"{base_url.rstrip('/')}/{device_type}/{hw_ver}/{target_version}"
    else:
        release_url = base_url

    try:
        led_status.set_status("OTA_UPDATE")
        if client:
            publish_status(client, "updating", f"OTA starting from {current_version} to {target_version or 'unknown'}")

        if cmd.get("manifest") is True:
            m_name = cmd.get("manifest_name") or ota_cfg.get("manifest", "manifest.json")
            print("📡 Fetching Manifest from:", release_url)
            manifest = fetch_manifest(release_url, m_name)
            success = ota_update(release_url, manifest=manifest)
        else:
            files = cmd.get("files", [])
            hashes = cmd.get("sha256", {})
            success = ota_update(release_url, files=files, hashes=hashes)

        if success and client:
            publish_status(client, "rebooting", "OTA Success - Rebooting")
            time.sleep(1)
            machine.reset()

    except Exception as e:
        print("❌ OTA Failed:", e)
        try:
            import display_manager
            display_manager.set_ota_status(None)
            display_manager.show_command_toast("OTA Failed!")
        except:
            pass
        led_status.set_status("MQTT_CONNECTED")

def mqtt_callback(topic, msg):
    global _pending_ota_cmd
    try:
        t = topic.decode()
        payload = ujson.loads(msg.decode())
    except: 
        return

    try:
        import display_manager
        display_manager.show_command_toast(payload.get("command", "UNKNOWN"))
    except:
        pass

    if payload.get("command") == "OTA":
        print("🚩 OTA Queued for execution...")
        _pending_ota_cmd = payload
    else:
        handle_command(payload)

def mqtt_thread(heartbeats=None):
    global _pending_ota_cmd
    _thread.stack_size(8192)
    
    cfg = config.load_config()
    client_cfg = cfg.get("client", {})
    mqtt_cfg = cfg.get("mqtt", {})
    
    client_id = client_cfg.get("id", "esp32_pump_01")
    server = mqtt_cfg.get("server", "10.10.10.211")
    port = int(mqtt_cfg.get("port", 1883))
    
    device_type = client_cfg.get("type", "pump")
    hw_ver = client_cfg.get("hardware_version", "esp32_1.0")
    pub_topic = f"{device_type}/{client_id}/telemetry"
    cmd_topic = f"{device_type}/{client_id}/command"
    broadcast_cmd_topic = f"{device_type}/{hw_ver}/command"
    
    PUBLISH_EVERY_SEC = int(mqtt_cfg.get("publish_every_sec", 5))

    last_pub_time = 0
    last_state = "OFF"

    while True:
        if heartbeats:
            heartbeats["mqtt"] = time.time()
        if not ensure_network_ready():
            time.sleep(2)
            continue

        try:
            client = MQTTClient(client_id, server, port, keepalive=60)
            client.set_callback(mqtt_callback)
            client.connect()
            client.subscribe(cmd_topic)
            client.subscribe(broadcast_cmd_topic)
            
            publish_status(client, "online", "Pump controller online")
            publish_version_announcement(client)
            print("✅ MQTT Connected to %s" % server)
            led_status.set_status("MQTT_CONNECTED")
            
            while True:
                if heartbeats: 
                    heartbeats["mqtt"] = time.time()
                
                client.check_msg() # Non-blocking check
                
                # Execute OTA if flag was set
                if _pending_ota_cmd:
                    run_ota_safely(client, _pending_ota_cmd)
                    _pending_ota_cmd = None

                now = time.time()
                
                # Immediate State change alert detection
                current_state = pump_controller.state
                if current_state != last_state:
                    event_msg = f"Pump switched from {last_state} to {current_state}"
                    if current_state == "TRIPPED":
                        publish_alert(client, "TRIP_FAULT", f"Pump tripped: {', '.join(pump_controller.active_faults)}")
                    else:
                        publish_alert(client, "STATE_CHANGE", event_msg)
                    last_state = current_state
                
                # Publish periodic telemetry
                if now - last_pub_time >= PUBLISH_EVERY_SEC:
                    pump_controller.lock.acquire()
                    try:
                        tele = dict(pump_controller.telemetry)
                        faults = list(pump_controller.active_faults)
                    finally:
                        pump_controller.lock.release()
                    
                    gps.lock.acquire()
                    try:
                        gdata = dict(gps.gps_data)
                    finally:
                        gps.lock.release()
                    
                    if tele:
                        payload = {
                            "timestamp": utc_iso(),
                            "motor_status": current_state,
                            "mode": tele.get("mode", "MANUAL"),
                            "voltages": [tele.get("v_a"), tele.get("v_b"), tele.get("v_c")],
                            "currents": [tele.get("i_a"), tele.get("i_b"), tele.get("i_c")],
                            "v_avg": tele.get("v_avg"),
                            "i_avg": tele.get("i_avg"),
                            "pressure_psi": tele.get("pressure"),
                            "flow_rate_gpm": tele.get("flow_rate"),
                            "est_kwh": round(pump_controller.est_kwh, 3),
                            "runtime_hours": round(pump_controller.runtime_sec / 3600.0, 2),
                            "daily_runtime_sec": int(pump_controller.daily_runtime_sec),
                            "tank_level": tele.get("tank_level_str"),
                            "feedback": tele.get("feedback"),
                            "estop": tele.get("estop"),
                            "active_faults": faults,
                            "gps": {
                                "lat": gdata.get("lat"),
                                "lon": gdata.get("lon"),
                                "sats": gdata.get("sats"),
                                "locked": gdata.get("locked"),
                                "speed_kmh": gdata.get("speed_kmh"),
                                "confidence_m": gdata.get("confidence_m")
                            }
                        }
                        client.publish(pub_topic, ujson.dumps(payload))
                        last_pub_time = now
                
                time.sleep(0.5)

        except Exception as e:
            print(f"⚠️ MQTT Link Lost: {e}. Retrying in 5s...")
            led_status.set_status("WIFI_CONNECTED")
            if heartbeats:
                heartbeats["mqtt"] = time.time()
            time.sleep(5)

def utc_iso():
    t = time.gmtime()
    return "%04d-%02d-%02dT%02d:%02d:%02dZ" % t[:6]
