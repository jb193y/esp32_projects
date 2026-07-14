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

def on_message(topic, msg):
    global _cmd_dispatcher
    try:
        topic_str = topic.decode('utf-8')
        payload_str = msg.decode('utf-8')
        print(f"📥 MQTT Received: Topic={topic_str}, Payload={payload_str}")
        
        payload = ujson.loads(payload_str)
        target = payload.get("target_node")
        command = payload.get("command")
        routing_path = payload.get("routing_path", [])
        args = payload.get("payload", {})
        
        if not target or not command:
            print("⚠️ MQTT command payload missing target_node or command")
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
    status_topic = f"{topic_prefix}/status"
    
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
                print(f"📡 Subscribed to command topic: {cmd_topic}")
                
                # Publish startup status
                publish_msg(status_topic, {
                    "client_id": client_id,
                    "status": "online",
                    "fw_ver": cfg.get("client", {}).get("firmware_version", "hub_v1.0.0")
                }, retain=True)
                
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
