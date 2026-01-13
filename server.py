# server.py
import usocket as socket
import ujson
import config
import network
import time
import machine

# Allowed top-level config sections that mobile app can modify
ALLOWED_SECTIONS = {
    "wifi",
    "mqtt",
    "gps",
    "app",
    "device",
    "time"
}

def start_server():
    """
    Setup server (AP mode)

    Endpoints:
      GET  /status   → device status + current config
      POST /update   → grouped config update (partial allowed)
    """
    s = socket.socket()
    s.bind(("0.0.0.0", 80))
    s.listen(1)
    print("📡 Setup server started on port 80")

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
                response = error("Invalid endpoint")

            send_json(conn, response)

        except Exception as e:
            print("❌ Server error:", e)
            try:
                send_json(conn, error(str(e)))
            except:
                pass
        finally:
            conn.close()

# -------------------------------------------------
# HANDLERS
# -------------------------------------------------

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
        "device": cfg.get("device"),
        "app": cfg.get("app"),
        "wifi": cfg.get("wifi"),
        "mqtt": cfg.get("mqtt"),
        "gps": cfg.get("gps"),
        "time": cfg.get("time"),
        "network": net_info
    }

def handle_update(request):
    try:
        body = request.split("\r\n\r\n", 1)[1]
        data = ujson.loads(body)
    except Exception:
        return error("Invalid JSON payload")

    if not isinstance(data, dict):
        return error("Payload must be a JSON object")

    cfg = config.load_config()
    updated = False

    for section, value in data.items():
        if section not in ALLOWED_SECTIONS:
            print("⚠️ Ignoring forbidden section:", section)
            continue

        if not isinstance(value, dict):
            print("⚠️ Ignoring non-dict section:", section)
            continue

        if section not in cfg or not isinstance(cfg[section], dict):
            cfg[section] = {}

        cfg[section].update(value)
        updated = True
        print("✅ Updated section:", section)

    if not updated:
        return error("No valid config sections updated")

    # Force STA mode after setup
    cfg.setdefault("device", {})["mode"] = "sta"

    config.save_config(cfg)

    return reboot_response("Config updated, rebooting to STA mode")

# -------------------------------------------------
# RESPONSES
# -------------------------------------------------

def reboot_response(msg):
    # Send response first, then reboot
    def delayed_reset():
        time.sleep(2)
        machine.reset()

    _start_delayed_reset()
    return {
        "status": "ok",
        "message": msg
    }

def _start_delayed_reset():
    import _thread
    _thread.start_new_thread(_reset_thread, ())

def _reset_thread():
    time.sleep(2)
    machine.reset()

def error(msg):
    return {
        "status": "error",
        "message": msg
    }

def send_json(conn, obj):
    payload = ujson.dumps(obj)
    conn.send(
        "HTTP/1.1 200 OK\r\n"
        "Content-Type: application/json\r\n"
        "Content-Length: %d\r\n"
        "\r\n" % len(payload)
    )
    conn.send(payload)
