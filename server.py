# server.py
import usocket as socket
import ujson
import config
import network
import time
import machine
import _thread

ALLOWED_SECTIONS = {"wifi", "mqtt", "gps", "client", "time", "ota", "server", "imu", "base"}

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
    s.bind(("0.0.0.0", port))
    s.listen(1)
    print("📡 Setup server started on port", port)

    while True:
        conn, addr = s.accept()
        print("🔌 Connection from:", addr)

        try:
            request = conn.recv(4096).decode()
            if not request:
                conn.close()
                continue

            method, path, *_ = request.split(" ", 2)
            print("HTTP", method, path)

            if method == "GET" and path == "/status":
                response = handle_status()
            elif method == "POST" and path == "/update":
                response = handle_update(request)
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
            conn.close()

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
        "base": cfg.get("base"),
        "wifi": cfg.get("wifi"),
        "mqtt": cfg.get("mqtt"),
        "gps": cfg.get("gps"),
        "time": cfg.get("time"),
        "ota": cfg.get("ota"),
        "server": cfg.get("server"),
        "network": net_info
    }

def handle_update(request):
    try:
        body = request.split("\r\n\r\n", 1)[1]
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

    # Validate: base.* only if resulting client.type == "base"
    current = config.load_config()
    current_client = current.get("client", {})
    new_client = dict(current_client)
    if "client" in patch:
        new_client.update(patch["client"])

    new_type = new_client.get("type", "rover")

    if "base" in patch and new_type != "base":
        return {"status": "error", "message": "base.* is only allowed when client.type == 'base'"}

    if new_type == "base":
        # ensure known_lat/lon exist either already or in patch
        base_now = dict(current.get("base", {}))
        base_now.update(patch.get("base", {}))
        if base_now.get("known_lat") is None or base_now.get("known_lon") is None:
            return {"status": "error", "message": "client.type='base' requires base.known_lat and base.known_lon"}

    # Apply update
    cfg = config.update_config(patch)

    # Force STA mode after provisioning
    cfg.setdefault("client", {})["mode"] = "sta"
    config.save_config(cfg)

    return reboot_response("Config updated. Rebooting...")

def send_json(conn, obj):
    payload = ujson.dumps(obj)
    conn.send(
        "HTTP/1.1 200 OK\r\n"
        "Content-Type: application/json\r\n"
        "Content-Length: %d\r\n"
        "\r\n" % len(payload)
    )
    conn.send(payload)
