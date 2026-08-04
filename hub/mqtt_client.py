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
        client_id = cfg.get("client", {}).get("id", "hub_master_01")
        client_type = cfg.get("client", {}).get("type", "hub").lower()
        tele_topic = f"{client_type}/{client_id}/telemetry"
        
        payload = {
            "timestamp": time.time(),
            "hub_status": status_val,
            "device_status": status_val,
            "mode": "AUTO"
        }
        publish_msg(tele_topic, payload)
        print(f"📊 Published Hub telemetry: {status_val} to {tele_topic}")
    except Exception as e:
        print("❌ Error publishing Hub telemetry:", e)

def on_message(topic, msg):
    global _cmd_dispatcher
    try:
        topic_str = topic.decode('utf-8')
        payload_str = msg.decode('utf-8')
        print(f"📥 MQTT Received: Topic={topic_str}, Payload={payload_str}")
        
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
            print("⚠️ MQTT command payload missing target_node or command")
            return

        cfg = config.load_config()
        client_id = cfg.get("client", {}).get("id", "hub_master_01")
        if target == client_id:
            if command in ("HUB_ENABLE", "HUB_DISABLE"):
                status_val = "Enabled" if command == "HUB_ENABLE" else "Disabled"
                config.update_config({"client": {"status": status_val}})
                publish_hub_telemetry(status_val)
            return
            
        if _cmd_dispatcher:
            # Dispatch to ESP-NOW mesh
            _cmd_dispatcher(target, command, routing_path, args)
        else:
            print("⚠️ No cmd_dispatcher registered")
    except Exception as e:
        print("❌ Error processing MQTT message:", e)

def publish_msg(topic, payload, retain=False):
    global _client, _is_connected
    if not _is_connected or _client is None:
        return False
    try:
        with _lock:
            _client.publish(topic.encode('utf-8'), ujson.dumps(payload).encode('utf-8'), retain=retain)
        return True
    except Exception as e:
        print("❌ MQTT publish error:", e)
        _is_connected = False
        return False

def mqtt_thread(heartbeats=None):
    global _client, _is_connected
    print("🚀 MQTT Client Thread Started")
    
    cfg = config.load_config()
    mqtt_cfg = cfg.get("mqtt", {})
    client_id = cfg.get("client", {}).get("id", "hub_master_01")
    topic_prefix = mqtt_cfg.get("topic_prefix", f"farm/{client_id}")
    
    cmd_topic = f"{topic_prefix}/command"
    # Status topic must match what mobile app subscribes to: {type}/{id}/status
    client_type = cfg.get("client", {}).get("type", "hub").lower()
    status_topic = f"{client_type}/{client_id}/status"
    
    while True:
        if heartbeats is not None:
            heartbeats["mqtt"] = time.time()
            
        if not network_manager.is_connected():
            _is_connected = False
            time.sleep(2)
            continue
            
        if not _is_connected:
            try:
                print(f"🔌 Connecting to MQTT Broker: {mqtt_cfg.get('server')}...")
                _client = MQTTClient(
                    client_id=client_id,
                    server=mqtt_cfg.get("server", "192.168.1.100"),
                    port=mqtt_cfg.get("port", 1883),
                    user=mqtt_cfg.get("user", ""),
                    password=mqtt_cfg.get("password", ""),
                    keepalive=mqtt_cfg.get("keepalive", 60)
                )
                _client.set_callback(on_message)
                _client.connect()
                _is_connected = True
                led_status.set_status("MQTT_CONNECTED")
                print("✅ MQTT Connected!")
                
                # Subscribe to command topic
                _client.subscribe(cmd_topic.encode('utf-8'))
                _client.subscribe(b"pump/+/command")
                _client.subscribe(b"valve/+/command")
                print(f"📡 Subscribed to command topics: {cmd_topic}, pump/+/command, valve/+/command")
                
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
                print("❌ MQTT connection failed:", e)
                _is_connected = False
                led_status.set_status("WIFI_CONNECTED")
                time.sleep(10)
                continue
                
        # Check for incoming messages
        try:
            _client.check_msg()
        except Exception as e:
            print("⚠️ MQTT connection error check:", e)
            _is_connected = False
            
        time.sleep(0.5)
