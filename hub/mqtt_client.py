# mqtt_client.py (Hub)
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
        location = client.get("location")
        group = client.get("group", "all")
        
        if location:
            tele_topic = f"{location}/{group}/{client_type}/{client_id}/telemetry"
        else:
            tele_topic = f"{client_type}/{client_id}/telemetry"
        
        payload = {
            "timestamp": time.time(),
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
        
        # Check if it matches mobile app format: {"device_id": "...", "state": {"pump": "ON"}}
        target = payload.get("device_id")
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
        elif "command" in payload:
            command = payload.get("command")
        else:
            # Fallback for old/direct testing format
            target = payload.get("target_node", target)
            command = payload.get("command")
            routing_path = payload.get("routing_path", [])
            args = payload.get("payload", {})
            
        # Fallback to extract target from topic if missing
        if not target and "/" in topic_str:
            parts = topic_str.split("/")
            if len(parts) >= 2:
                target = parts[1]
        
        if not target or not command:
            print("MQTT command payload missing target_node or command")
            return

        cfg = config.load_config()
        client_id = cfg.get("client", {}).get("id", "hub_master_01")

        # Instant MQTT Acknowledgment back to sender
        resp_payload = {
            "status": "RECEIVED_BY_HUB",
            "target_node": target,
            "command": command,
            "timestamp": time.time(),
            "hub_id": client_id
        }
        publish_msg(f"{topic_str}/response", resp_payload)
        publish_msg(f"farm/{client_id}/command_response", resp_payload)
        print(f"Published RECEIVED_BY_HUB ACK for {target}:{command}")

        if target == client_id:
            if command in ("HUB_ENABLE", "HUB_DISABLE"):
                status_val = "Enabled" if command == "HUB_ENABLE" else "Disabled"
                config.update_config({"client": {"status": status_val}})
                publish_hub_telemetry(status_val)
            elif command in ("BLINK_LED", "COM_TEST"):
                print("Visual COM_TEST / BLINK_LED triggered on Hub!")
                _thread.start_new_thread(_blink_hub_led_bg, ())
            return

        if _cmd_dispatcher:
            _cmd_dispatcher(target, command, routing_path, args)
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
    location = client_info.get("location")
    group = client_info.get("group", "all")
    
    topic_prefix = mqtt_cfg.get("topic_prefix", f"farm/{client_id}")
    cmd_topic = f"{topic_prefix}/command"
    status_topic = f"{client_type}/{client_id}/status"
    if location:
        status_topic = f"{location}/{group}/{client_type}/{client_id}/status"
    
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
                _client.set_callback(on_message)
                _client.connect()
                _is_connected = True
                led_status.set_status("MQTT_CONNECTED")
                print(" MQTT Connected!")
                
                # Subscribe to command topics
                _client.subscribe(cmd_topic.encode('utf-8'))
                _client.subscribe(b"pump/+/command")
                _client.subscribe(b"valve/+/command")
                _client.subscribe(b"+/+/command")
                _client.subscribe(b"+/+/+/+/command")
                print(f" Subscribed to command topics: {cmd_topic}, pump/+/command, valve/+/command, +/+/command, +/+/+/+/command")

                if hasattr(_client, 'sock') and _client.sock:
                    try:
                        _client.sock.setblocking(False)
                    except Exception:
                        pass
                
                # Publish startup status
                publish_msg(status_topic, {
                    "client_id": client_id,
                    "status": "online",
                    "timestamp": time.time(),
                    "fw_ver": cfg.get("client", {}).get("firmware_version", "hub_v1.0.0")
                }, retain=True)
                
                # Publish initial Hub status telemetry
                hub_status = cfg.get("client", {}).get("status", "Enabled")
                publish_hub_telemetry(hub_status)
                
            except Exception as e:
                print("MQTT connection failed:", e)
                _is_connected = False
                led_status.set_status("WIFI_CONNECTED")
                time.sleep(10)
                continue
                
        # Check for incoming messages non-blockingly
        try:
            _client.check_msg()
        except Exception as e:
            err_num = getattr(e, 'errno', None)
            if err_num not in (11, 110):
                print("MQTT connection error check:", e)
                _is_connected = False
            
        time.sleep(0.1)
