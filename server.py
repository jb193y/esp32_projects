# server.py
import usocket as socket
import ujson
import config
import network
import time
import machine
import _thread
import ubinascii

ALLOWED_SECTIONS = {"wifi", "mqtt", "gps", "client", "time", "ota", "server", "pump", "display"}

def reboot_response(msg):
    _start_delayed_reset()
    return {"status": "ok", "message": msg}

def _start_delayed_reset():
    _thread.start_new_thread(_reset_thread, ())

def _reset_thread():
    time.sleep(2)
    machine.reset()

def start_server():
    cfg = config.load_config()
    port = int(cfg.get("server", {}).get("port", 80))

    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", port))
    s.listen(1)
    print("📡 Setup server started on port", port)

    while True:
        try:
            conn, addr = s.accept()
            conn.settimeout(5.0)
            
            request = conn.recv(4096).decode()
            if not request:
                conn.close()
                continue

            parts = request.split(" ", 2)
            if len(parts) < 2:
                conn.close()
                continue
            method, path = parts[0], parts[1]
            print("HTTP", method, path)

            # Handle CORS OPTIONS Preflight
            if method == "OPTIONS":
                send_options_response(conn)
                conn.close()
                continue

            if method == "GET" and path == "/status":
                response = handle_status()
            elif method == "GET" and path == "/info":
                response = handle_info()
            elif method == "POST" and path == "/update":
                response = handle_update(request)
            elif method == "POST" and (path == "/setup" or path == "/api/setup"):
                response = handle_setup_post(request)
            elif method == "POST" and path == "/provision":
                response = handle_provision(request)
            else:
                response = {"status": "error", "message": "Invalid endpoint"}

            send_json(conn, response)

        except Exception as e:
            print("❌ Server error:", e)
            try:
                send_json(conn, {"status": "error", "message": str(e)})
            except:
                pass
        finally:
            try:
                conn.close()
            except:
                pass

def handle_status():
    cfg = config.load_config()
    sta = network.WLAN(network.STA_IF)
    ap = network.WLAN(network.AP_IF)

    net_info = None
    if sta.isconnected():
        net_info = sta.ifconfig()
    elif ap.active():
        net_info = ap.ifconfig()

    return {
        "status": "ok",
        "client": cfg.get("client"),
        "wifi": cfg.get("wifi"),
        "mqtt": cfg.get("mqtt"),
        "gps": cfg.get("gps"),
        "time": cfg.get("time"),
        "ota": cfg.get("ota"),
        "server": cfg.get("server"),
        "pump": cfg.get("pump"),
        "display": cfg.get("display"),
        "network": net_info
    }

def handle_info():
    """Returns device identification details for the mobile app setup wizard."""
    cfg = config.load_config()
    client_cfg = cfg.get("client", {})
    
    # Get MAC Address
    wlan = network.WLAN(network.STA_IF)
    try:
        mac_bytes = wlan.config('mac')
        mac_str = ":".join(["%02x" % b for b in mac_bytes])
    except:
        mac_str = "00:00:00:00:00:00"
        
    device_id = client_cfg.get("id", "esp32_pump_01")
    device_type = client_cfg.get("type", "pump")
    serial_number = client_cfg.get("serial_number", "SN-UNKNOWN")
    model = client_cfg.get("model", "MODEL-UNKNOWN")
    
    return {
        "device_id": device_id,
        "device_type": device_type,
        "default_name": f"Agripulse {device_type.capitalize()} Controller",
        "mac": mac_str,
        "serial_number": serial_number,
        "sn": serial_number,
        "model": model
    }

def handle_update(request):
    try:
        parts = request.split("\r\n\r\n", 1)
        body = parts[1] if len(parts) > 1 else ""
        data = ujson.loads(body)
    except Exception:
        return {"status": "error", "message": "Invalid JSON payload"}

    if not isinstance(data, dict):
        return {"status": "error", "message": "Payload must be a JSON object"}

    # Filter allowed groups only
    patch = {}
    for group, group_patch in data.items():
        if group not in ALLOWED_SECTIONS:
            continue
        if not isinstance(group_patch, dict):
            continue
        patch[group] = group_patch

    if not patch:
        return {"status": "error", "message": "No valid config sections provided"}

    # Apply update
    cfg = config.update_config(patch)

    # Force STA mode after provisioning
    cfg.setdefault("client", {})["mode"] = "sta"
    config.save_config(cfg)

    return reboot_response("Config updated. Rebooting...")

def handle_setup_post(request):
    """
    Legacy Endpoint: POST /api/setup
    Payload: {"wifi_ssid": "...", "wifi_pass": "...", "mqtt_broker": "...", "client_id": "...", "pump_mode": "..."}
    """
    try:
        parts = request.split("\r\n\r\n", 1)
        body = parts[1] if len(parts) > 1 else ""
        data = ujson.loads(body)
        
        config_patch = {}
        
        # Configure WiFi
        if 'wifi_ssid' in data:
            ssid = data['wifi_ssid']
            password = data.get('wifi_pass', '')
            config_patch["wifi"] = {"networks": [{"ssid": ssid, "password": password}]}
            
        # Configure MQTT broker
        if 'mqtt_broker' in data:
            config_patch["mqtt"] = {"server": data['mqtt_broker']}
            
        # Configure client ID
        if 'client_id' in data:
            config_patch["client"] = {"id": data['client_id']}
            
        # Configure pump mode
        if 'pump_mode' in data:
            config_patch.setdefault("pump", {})["mode"] = data['pump_mode']
            
        if not config_patch:
            return {"status": "error", "message": "No setup data provided"}

        # Force station mode for next boot
        config_patch.setdefault("client", {})["mode"] = "sta"
        
        # Save and trigger reboot
        config.update_config(config_patch)
        return reboot_response("Provisioning complete. Rebooting to client mode...")
    except Exception as e:
        return {"status": "error", "message": str(e)}

def handle_provision(request):
    """
    Agripulse App Endpoint: POST /provision
    Payload: {"ssid": "...", "password": "...", "mqtt_broker": "...", "mqtt_port": 1883, "mqtt_topic": "..."}
    """
    try:
        parts = request.split("\r\n\r\n", 1)
        body = parts[1] if len(parts) > 1 else ""
        data = ujson.loads(body)
        
        config_patch = {}
        
        # WiFi configuration
        if 'ssid' in data:
            ssid = data['ssid']
            password = data.get('password', '')
            config_patch["wifi"] = {"networks": [{"ssid": ssid, "password": password}]}
            
        # MQTT Broker configuration
        mqtt_update = {}
        if 'mqtt_broker' in data:
            mqtt_update["server"] = data['mqtt_broker']
        if 'mqtt_port' in data:
            mqtt_update["port"] = int(data['mqtt_port'])
        if 'mqtt_topic' in data:
            base_topic = data['mqtt_topic'].rstrip('/')
            # Map the base topic to specific telemetry and command sub-topics
            mqtt_update["publish_topic"] = f"{base_topic}/telemetry"
            mqtt_update["command_topic"] = f"{base_topic}/command"
            
        if mqtt_update:
            config_patch["mqtt"] = mqtt_update
            
        if not config_patch:
            return {"status": "error", "message": "No configuration provided"}

        # Force station mode for next boot to join the newly provisioned router network
        config_patch.setdefault("client", {})["mode"] = "sta"
        
        # Save and trigger reboot
        config.update_config(config_patch)
        return reboot_response("Provisioning complete. Rebooting to client mode...")
    except Exception as e:
        return {"status": "error", "message": str(e)}
    
def send_options_response(conn):
    """Send CORS headers for preflight request."""
    conn.send(
        "HTTP/1.1 204 No Content\r\n"
        "Access-Control-Allow-Origin: *\r\n"
        "Access-Control-Allow-Methods: GET, POST, OPTIONS\r\n"
        "Access-Control-Allow-Headers: Content-Type\r\n"
        "Content-Length: 0\r\n"
        "\r\n"
    )

def send_json(conn, obj):
    payload = ujson.dumps(obj)
    conn.send(
        "HTTP/1.1 200 OK\r\n"
        "Content-Type: application/json\r\n"
        "Access-Control-Allow-Origin: *\r\n"
        "Content-Length: %d\r\n"
        "\r\n" % len(payload)
    )
    conn.send(payload)
