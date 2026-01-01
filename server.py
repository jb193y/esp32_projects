import usocket as socket
import ujson
import config
import wifiap
import network
import time
import machine

def start_server():
    """
    Minimal REST-like setup server for mobile app configuration.
    Endpoints:
      GET /status  → Returns device info & config
      POST /update → Updates config and switches to STA mode
    """
    s = socket.socket()
    s.bind(('0.0.0.0', 80))
    s.listen(1)
    print("Setup server started on port 80")

    while True:
        conn, addr = s.accept()
        print("Connection from:", addr)

        try:
            request = conn.recv(2048).decode()
            if not request:
                conn.close()
                continue

            # Basic request parsing
            method, path, *_ = request.split(" ", 2)
            print(f"HTTP {method} {path}")

            if method == "GET" and path == "/status":
                cfg = config.load_config()
                wlan = network.WLAN(network.STA_IF)
                ap = network.WLAN(network.AP_IF)
                response = {
                    "client_id": cfg.get("client_id"),
                    "mode": cfg.get("mode"),
                    "wifi_networks": cfg.get("wifi_networks", []),
                    "ip": wlan.ifconfig() if wlan.isconnected() else ap.ifconfig()
                }

            elif method == "POST" and path == "/update":
                try:
                    body = request.split('\r\n\r\n', 1)[1]
                    data = ujson.loads(body)

                    # Update config file
                    cfg = config.update_config(data)
                    cfg["mode"] = "sta"
                    config.save_config(cfg)

                    response = {"status": "ok", "message": "Config updated. Restarting to apply."}
                    send_json(conn, response)

                    # Wait a moment and restart device in STA mode
                    time.sleep(2)
                    machine.reset()
                    continue

                except Exception as e:
                    response = {"status": "error", "message": str(e)}

            else:
                response = {"status": "error", "message": "Invalid endpoint"}

            send_json(conn, response)

        except Exception as e:
            print("Error handling request:", e)
            try:
                send_json(conn, {"status": "error", "message": str(e)})
            except:
                pass

        finally:
            conn.close()


def send_json(conn, obj):
    """Send a JSON response with proper headers."""
    payload = ujson.dumps(obj)
    conn.send("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n")
    conn.send(payload)
