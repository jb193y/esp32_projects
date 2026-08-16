# mqtt_client.py (Hub)
import sys
import usocket

# Enforce a 3.0-second socket timeout on all connection sockets to prevent
# blocking MQTT connects from starving CPU cores and disrupting ESP-NOW.
class TimeoutSocket(usocket.socket):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.settimeout(3.0)

class UsocketWrapper:
    def __init__(self, orig):
        self._orig = orig
    def __getattr__(self, name):
        if name == 'socket':
            return TimeoutSocket
        return getattr(self._orig, name)

sys.modules['usocket'] = UsocketWrapper(usocket)
sys.modules['socket'] = sys.modules['usocket']

from umqtt.simple import MQTTClient
import ujson
import time
import _thread
import config
import led_status
import network_manager

_client = None
_lock = _thread.allocate_lock()
_is_connected = False
_provision_confirmed = False
_timer_started = False

def _provision_confirmation_timer_bg():
    global _provision_confirmed
    print(" Provision confirmation countdown started (90s window)...")
    try:
        led_status.set_status("START_DISCOVERY")
    except Exception:
        pass

    start_t = time.time()
    while time.time() - start_t < 90:
        if _provision_confirmed:
            print(" Provision confirmation timer canceled (confirmed successfully!).")
            return
        time.sleep(1)

    if not _provision_confirmed:
        print(" Provision confirmation TIMEOUT (90s elapsed without API claim confirmation)!")
        print(" Auto-resetting device back to BLE Provisioning mode...")
        try:
            cfg = config.load_config()
            cfg.setdefault("client", {})["mode"] = "ble_setup"
            with open("config.json", "w") as f:
                ujson.dump(cfg, f)
        except Exception as ex:
            print(" Config reset notice:", ex)

        try:
            import sys
            sys.stdout.write("\r\n--- REBOOTING ESP32 TO BLE SETUP ---\r\n")
            sys.stdout.flush()
        except Exception:
            pass
        time.sleep_ms(300)

        try:
            import machine
            machine.reset()
        except Exception:
            pass

# Callback from esp_now_master to dispatch commands
_cmd_dispatcher = None

def register_cmd_dispatcher(dispatcher):
    global _cmd_dispatcher
    _cmd_dispatcher = dispatcher

def is_connected():
    return _is_connected

def publish_hub_telemetry(status_val):
    try:
        cfg = config.load_config()
        client = cfg.get("client", {})
        client_id = client.get("id", "hub_master_01")
        client_type = client.get("type", "hub").lower()
        site = client.get("site", "default_site")
        group = client.get("group", "all")
        
        if site == "default_site":
            print("ERROR: 'site' not set in config. Cannot publish hub telemetry.")
            return
        
        tele_topic = f"{site}/{group}/{client_type}/{client_id}/telemetry"

        payload = {
            "timestamp": config.get_unix_time(),
            "hub_status": status_val,
            "device_status": status_val,
            "mode": "AUTO"
        }
        publish_msg(tele_topic, payload)
        print(f"Published Hub telemetry: {status_val} to {tele_topic}")
    except Exception as e:
        print("Error publishing Hub telemetry:", e)

def on_message(topic, msg):
    global _cmd_dispatcher
    try:
        topic_str = topic.decode('utf-8')
        payload_str = msg.decode('utf-8')
        print(f"MQTT Received: Topic={topic_str}, Payload={payload_str}")
        
        payload = ujson.loads(payload_str)
        
        # Check if it is the standardized JSON envelope
        if "source" in payload and "target" in payload and "msg_type" in payload and "data" in payload:
            target_device = payload.get("target")
            msg_type = payload.get("msg_type")
            data = payload.get("data", {})
            
            command = data.get("cmd") or data.get("command") or data.get("state")
            routing_path = payload.get("route", {}).get("hops", [])
            args = data
            
            # If data is a string (legacy command representation), unpack it
            if isinstance(data, str):
                command = data
                args = {}
            elif isinstance(data, dict):
                # Ensure we get the command if nested under "state"
                if "state" in data:
                    state_data = data["state"]
                    if isinstance(state_data, dict):
                        if "pump" in state_data:
                            command = "PUMP_ON" if state_data["pump"] == "ON" else "PUMP_OFF"
                        elif "valve" in state_data:
                            command = "VALVE_OPEN" if state_data["valve"] in ("OPEN", "ON") else "VALVE_CLOSE"
                        elif "hub_status" in state_data:
                            command = "HUB_ENABLE" if state_data["hub_status"] == "Enabled" else "HUB_DISABLE"
                        elif "command" in state_data:
                            command = state_data["command"]
                        elif "cmd" in state_data:
                            command = state_data["cmd"]
            
            # Resolve to string if command is still a dictionary
            if isinstance(command, dict):
                command = command.get("cmd") or command.get("command")
        else:
            # Fallback legacy parsing
            target_device = payload.get("device_id")
            command = None
            args = {}
            routing_path = []
            
            if "state" in payload:
                state_data = payload.get("state", {})
                if "pump" in state_data:
                    if topic_str.startswith("valve/"):
                        command = "VALVE_OPEN" if state_data["pump"] == "ON" else "VALVE_CLOSE"
                    else:
                        command = "PUMP_ON" if state_data["pump"] == "ON" else "PUMP_OFF"
                elif "valve" in state_data:
                    command = "VALVE_OPEN" if state_data["valve"] in ("OPEN", "ON") else "VALVE_CLOSE"
                elif "hub_status" in state_data:
                    command = "HUB_ENABLE" if state_data["hub_status"] == "Enabled" else "HUB_DISABLE"
                elif "command" in state_data:
                    command = state_data["command"]
                    target_device = state_data.get("target_device_id") or state_data.get("target_node") or target_device
                    args = {k: v for k, v in state_data.items() if k not in ("command", "target_node", "target_device_id")}
            elif "command" in payload:
                command = payload.get("command")
                target_device = payload.get("target_device_id") or payload.get("target_node") or target_device
                routing_path = payload.get("routing_path", [])
                args = {k: v for k, v in payload.items() if k not in ("command", "target_node", "target_device_id", "routing_path")}
                if "payload" in payload and isinstance(payload["payload"], dict):
                    args.update(payload["payload"])
            else:
                target_device = payload.get("target_node", target_device)
                command = payload.get("command")
                routing_path = payload.get("routing_path", [])
                args = payload.get("payload", {})
            
        # Fallback to extract target from topic if still missing
        if not target_device and "/" in topic_str:
            parts = topic_str.split("/")
            if len(parts) >= 4 and parts[-1] == "command": # e.g. location/group/pump/device123/command
                target_device = parts[-2]
        
        if not target_device or not command:
            print("MQTT command payload missing target_device_id or command")
            return

        cfg = config.load_config()
        client_id = cfg.get("client", {}).get("id", "hub_master_01")

        # Instant MQTT Acknowledgment back to sender inside standard envelope
        resp_payload = {
            "source": client_id,
            "target": payload.get("source", "backend_api"),
            "msg_type": "ACK",
            "timestamp": config.get_unix_time(),
            "route": {
                "transport": "MQTT",
                "route_id": "hub_ack",
                "current_hop_index": 0,
                "hops": [payload.get("source", "backend_api")],
                "link_diagnostics": []
            },
            "data": {
                "status": "RECEIVED_BY_HUB",
                "target_device_id": target_device,
                "command": command
            }
        }
        publish_msg(f"{topic_str}/response", resp_payload)
        publish_msg(f"farm/{client_id}/command_response", resp_payload)
        print(f"Published RECEIVED_BY_HUB ACK for {target_device}:{command}")

        if target_device == client_id:
            if command in ("CONFIRM_PROVISION", "confirm_provision"):
                global _provision_confirmed
                _provision_confirmed = True
                print(" Provisioning confirmed via MQTT! System fully operational.")
                config.update_config({"client": {"mode": "normal"}})
                led_status.set_status("MQTT_CONNECTED")
                publish_hub_telemetry("PROVISION_CONFIRMED")
            elif command in ("HUB_ENABLE", "HUB_DISABLE"):
                status_val = "Enabled" if command == "HUB_ENABLE" else "Disabled"
                config.update_config({"client": {"status": status_val}})
                publish_hub_telemetry(status_val)
            elif command in ("BLINK_LED", "COM_TEST"):
                print("Visual COM_TEST / BLINK_LED triggered on Hub!")
                _thread.start_new_thread(_blink_hub_led_bg, ())
            elif command in ("START_DISCOVERY", "START_MESH_DISCOVERY"):
                if _cmd_dispatcher:
                    _cmd_dispatcher(target_device, command, routing_path, args)
            return

        if _cmd_dispatcher:
            _cmd_dispatcher(target_device, command, routing_path, args)
        else:
            print("No cmd_dispatcher registered")
    except Exception as e:
        print("Error processing MQTT message:", e)

def _blink_hub_led_bg():
    try:
        prev_st = getattr(led_status, "_state", "MQTT_CONNECTED")
        led_status.set_status("BLE_PROVISIONING")
        time.sleep(3)
        led_status.set_status(prev_st)
        publish_hub_telemetry("BLINK_COMPLETE")
    except Exception as ex:
        print("Blink BG Error:", ex)

def publish_msg(topic, payload, retain=False):
    global _client, _is_connected
    if not _is_connected or _client is None:
        return False
    try:
        _lock.acquire()
        try:
            _client.publish(topic.encode('utf-8'), ujson.dumps(payload).encode('utf-8'), retain=retain)
        finally:
            _lock.release()
        return True
    except Exception as e:
        print(" MQTT publish error:", e)
        _is_connected = False
        return False

def mqtt_thread(heartbeats=None):
    global _client, _is_connected
    print(" MQTT Client Thread Started")
    
    cfg = config.load_config()
    mqtt_cfg = cfg.get("mqtt", {})
    client_info = cfg.get("client", {})
    client_id = client_info.get("id", "hub_master_01")
    client_type = client_info.get("type", "hub").lower()
    site = client_info.get("site", "default_site")
    group = client_info.get("group", "all")
    
    # The hub's own command topic, using the new namespaced format
    cmd_topic = f"{site}/{group}/{client_type}/{client_id}/command"
    status_topic = f"{site}/{group}/{client_type}/{client_id}/status"
    
    while True:
        if heartbeats is not None:
            heartbeats["mqtt"] = time.time()
            
        if not network_manager.is_connected():
            _is_connected = False
            time.sleep(2)
            continue
            
        if not _is_connected:
            try:
                broker_host = mqtt_cfg.get("server") or mqtt_cfg.get("broker") or "10.10.10.211"
                print(f"Connecting to MQTT Broker: {broker_host}...")
                _client = MQTTClient(
                    client_id=client_id,
                    server=broker_host,
                    port=mqtt_cfg.get("port", 1883),
                    user=mqtt_cfg.get("user", ""),
                    password=mqtt_cfg.get("password", ""),
                    keepalive=mqtt_cfg.get("keepalive", 60)
                )
                
                # Configure Last Will and Testament (LWT) for abrupt disconnects
                lwt_payload = ujson.dumps({
                    "client_id": client_id,
                    "status": "offline",
                    "timestamp": config.get_unix_time(),
                    "reason": "keepalive_timeout"
                })
                try:
                    _client.set_last_will(status_topic.encode('utf-8'), lwt_payload.encode('utf-8'), retain=True)
                except Exception as lwt_err:
                    print("Failed to set Last Will:", lwt_err)

                _client.set_callback(on_message)
                _client.connect()
                _is_connected = True
                led_status.set_status("MQTT_CONNECTED")
                print(" MQTT Connected!")
                
                # Subscribe to command topics
                _client.subscribe(cmd_topic.encode('utf-8'))
                # Subscribe to all pump and valve commands using the new namespaced format
                _client.subscribe(b"+/+/pump/+/command")
                _client.subscribe(b"+/+/valve/+/command")
                print(f" Subscribed to namespaced command topics: {cmd_topic}, +/+/pump/+/command, ...")

                if hasattr(_client, 'sock') and _client.sock:
                    try:
                        _client.sock.setblocking(False)
                    except Exception:
                        pass
                
                # Publish startup status
                if site != "default_site":
                    publish_msg(status_topic, {
                        "client_id": client_id,
                        "status": "online",
                        "timestamp": config.get_unix_time(),
                        "fw_ver": cfg.get("client", {}).get("firmware_version", "hub_v1.0.0")
                    }, retain=True)
                else:
                    print("ERROR: 'site' not set in config. Cannot publish hub status.")
                
                # Publish initial Hub status telemetry
                hub_status = cfg.get("client", {}).get("status", "Enabled")
                publish_hub_telemetry(hub_status)

                # Check if device is pending provision confirmation
                global _timer_started
                client_mode = cfg.get("client", {}).get("mode", "normal")
                if client_mode != "normal" and not _provision_confirmed and not _timer_started:
                    _timer_started = True
                    print(" Waiting for MQTT command 'CONFIRM_PROVISION' from backend API...")
                    _thread.start_new_thread(_provision_confirmation_timer_bg, ())
                
            except Exception as e:
                print("MQTT connection failed:", e)
                _is_connected = False
                led_status.set_status("WIFI_CONNECTED")
                time.sleep(10)
                continue
                
        # Check for incoming messages non-blockingly
        try:
            _lock.acquire()
            try:
                _client.check_msg()
            finally:
                _lock.release()
        except Exception as e:
            err_num = getattr(e, 'errno', None)
            if err_num not in (11, 110):
                print("MQTT connection error check:", e)
                _is_connected = False
            
        time.sleep(0.1)
