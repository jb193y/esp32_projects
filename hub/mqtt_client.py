# mqtt_client.py (Hub)
import sys
import usocket

# Enforce a 3.0-second socket timeout on all connection sockets to prevent
# blocking MQTT connects from starving CPU cores and disrupting ESP-NOW.
def TimeoutSocket(*args, **kwargs):
    sock = usocket.socket(*args, **kwargs)
    try:
        sock.settimeout(3.0)
    except Exception:
        pass
    return sock

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

class ReentrantLock:
    def __init__(self):
        self._lock = _thread.allocate_lock()
        self._owner = None
        self._count = 0

    def acquire(self):
        tid = _thread.get_ident()
        if self._owner == tid:
            self._count += 1
            return True
        self._lock.acquire()
        self._owner = tid
        self._count = 1
        return True

    def release(self):
        tid = _thread.get_ident()
        if self._owner != tid:
            raise RuntimeError("Cannot release lock owned by another thread")
        self._count -= 1
        if self._count == 0:
            self._owner = None
            self._lock.release()

_client = None
_lock = ReentrantLock()
_is_connected = False
_enabled = True
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
            machine.soft_reset()
        except Exception:
            pass

# Callback from espnow_master to dispatch commands
_cmd_dispatcher = None

def register_cmd_dispatcher(dispatcher):
    global _cmd_dispatcher
    _cmd_dispatcher = dispatcher

def is_connected():
    return _is_connected

def set_enabled(enabled):
    global _enabled, _client, _is_connected
    _enabled = bool(enabled)
    if not _enabled:
        _client = None
        _is_connected = False

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
            "source": client_id,
            "target": "backend_api",
            "msg_type": "TELEMETRY",
            "timestamp": config.get_unix_time(),
            "route": {
                "transport": "MQTT",
                "route_id": "direct",
                "current_hop_index": 0,
                "hops": [client_id],
                "link_diagnostics": []
            },
            "data": {
                "device_id": client_id,
                "hub_status": status_val,
                "device_status": status_val,
                "mode": "AUTO"
            }
        }
        publish_msg(tele_topic, payload)
        print(f"Published Hub telemetry: {status_val} to {tele_topic}")
    except Exception as e:
        print("Error publishing Hub telemetry:", e)

def publish_hub_schedules():
    try:
        import scheduler
        schedules_list = scheduler.load_schedules()
        
        cfg = config.load_config()
        client_info = cfg.get("client", {})
        client_id = client_info.get("id", "hub_master_01")
        client_type = client_info.get("type", "hub").lower()
        site = client_info.get("site", "default_site")
        group = client_info.get("group", "all")
        
        topic = f"{site}/{group}/{client_type}/{client_id}/schedules"
        
        payload = {
            "source": client_id,
            "target": "backend_api",
            "msg_type": "SCHEDULES",
            "timestamp": config.get_unix_time(),
            "route": {
                "transport": "MQTT",
                "route_id": "direct",
                "current_hop_index": 0,
                "hops": [client_id],
                "link_diagnostics": []
            },
            "data": {
                "schedules": schedules_list
            }
        }
        publish_msg(topic, payload)
        print(f"Published Hub schedules to {topic}")
    except Exception as e:
        print("Error publishing schedules:", e)

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
            
            if topic_str.endswith("/config"):
                cfg = config.load_config()
                client_id = cfg.get("client", {}).get("id", "hub_master_02")
                if target_device == client_id:
                    try:
                        settings = data.get("settings", {})
                        
                        # Extract and parse Well Recharge Delay
                        well_recharge = settings.get("Well & Water Management", {}).get("Well Recharge Delay", "2 Hours")
                        well_recharge_sec = 1800
                        if "1" in well_recharge: well_recharge_sec = 3600
                        elif "2" in well_recharge: well_recharge_sec = 7200
                        elif "4" in well_recharge: well_recharge_sec = 14400
                        elif "6" in well_recharge: well_recharge_sec = 21600
                        elif "8" in well_recharge: well_recharge_sec = 28800
                        
                        wifi_ssid = settings.get("Network Configuration", {}).get("WiFi SSID")
                        
                        update_payload = {}
                        if wifi_ssid:
                            networks = cfg.get("wifi", {}).get("networks", [])
                            if networks:
                                networks[0]["ssid"] = wifi_ssid
                                update_payload["wifi"] = {"networks": networks}
                                
                        update_payload["scheduler"] = {
                            "well_recharge_delay_sec": well_recharge_sec
                        }
                        
                        config.update_config(update_payload)
                        print(" HUB configurations updated from MQTT successfully.")
                    except Exception as config_err:
                        print("Error processing config update on HUB:", config_err)
                return
            
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
                        # Merge state keys to top level of args to keep ESP-NOW payloads flat and compact
                        for k, v in state_data.items():
                            if k not in args:
                                args[k] = v
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
            print("Ignoring legacy or non-envelope MQTT payload:", payload_str)
            return
        
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
        if topic_str.endswith("/command"):
            ack_topic = topic_str[:-8] + "/acks"
        else:
            ack_topic = f"{topic_str}/acks"
        publish_msg(ack_topic, resp_payload)
        print(f"Published RECEIVED_BY_HUB ACK for {target_device}:{command} to {ack_topic}")

        if target_device == client_id:
            if command in ("UPDATE_SCHEDULE", "ADD_SCHEDULE"):
                try:
                    import scheduler
                    sched_dict = args.get("schedule") or args
                    res, msg = scheduler.add_or_update_schedule(sched_dict)
                    print(f"Schedule update result: {res} ({msg})")
                    publish_hub_schedules()
                except Exception as ex:
                    print("Error updating schedule:", ex)
                return
            elif command in ("DELETE_SCHEDULE", "REMOVE_SCHEDULE"):
                try:
                    import scheduler
                    sched_id = args.get("schedule_id")
                    res, msg = scheduler.remove_schedule(sched_id)
                    print(f"Schedule remove result: {res} ({msg})")
                    publish_hub_schedules()
                except Exception as ex:
                    print("Error removing schedule:", ex)
                return
            elif command in ("RESOURCE_SURPLUS", "RESOURCE_EVENT"):
                try:
                    import scheduler
                    res_name = args.get("resource") or args.get("resource_name") or "solar"
                    status = args.get("status") or "surplus"
                    duration = int(args.get("duration_sec") or args.get("duration") or 3600)
                    
                    if status in ("surplus", "active", "available"):
                        scheduler.set_resource_surplus(res_name, duration)
                    else:
                        scheduler.set_resource_surplus(res_name, 0)
                except Exception as ex:
                    print("Error handling resource surplus command:", ex)
                return
            elif command in ("CONFIRM_PROVISION", "confirm_provision"):
                global _provision_confirmed
                _provision_confirmed = True
                print(" Provisioning confirmed via MQTT! System fully operational.")
                config.update_config({"client": {"mode": "normal"}})
                cfg.setdefault("client", {})["mode"] = "normal"
                led_status.set_status("MQTT_CONNECTED")
                
                # Publish official online status and initial telemetry
                publish_msg(status_topic, {
                    "source": client_id,
                    "target": "backend_api",
                    "msg_type": "STATUS",
                    "timestamp": config.get_unix_time(),
                    "route": {
                        "transport": "MQTT",
                        "route_id": "direct",
                        "current_hop_index": 0,
                        "hops": [client_id],
                        "link_diagnostics": []
                    },
                    "data": {
                        "device_id": client_id,
                        "status": "online",
                        "fw_ver": cfg.get("client", {}).get("firmware_version", "hub_v1.0.0")
                    }
                }, retain=True)
                publish_hub_telemetry("Enabled")
            elif command in ("HUB_ENABLE", "HUB_DISABLE"):
                status_val = "Enabled" if command == "HUB_ENABLE" else "Disabled"
                config.update_config({"client": {"status": status_val}})
                publish_hub_telemetry(status_val)
            elif command == "COM_TEST":
                print("Visual COM_TEST triggered on Hub!")
                _thread.start_new_thread(_blink_hub_led_bg, ())
            elif command in ("START_DISCOVERY", "START_MESH_DISCOVERY"):
                if _cmd_dispatcher:
                    _cmd_dispatcher(target_device, command, routing_path, args)
            elif command == "OTA":
                print("🚀 OTA command received! Initiating firmware update...")
                try:
                    import ota
                    client_info = cfg.get("client", {})
                    ota_cfg = cfg.get("ota", {})
                    ota_url = args.get("url") or ota_cfg.get("base_url") or "http://10.10.10.211:8000/fw"
                    manifest_name = args.get("manifest_name") or ota_cfg.get("manifest") or "manifest.json"
                    
                    fw_ver = args.get("version") or client_info.get("firmware_version", "hub_v1.0.0")
                    base_url = ota_url.rstrip('/')
                    
                    print(f"📡 Downloading OTA manifest from: {base_url}/{manifest_name}")
                    manifest = ota.fetch_manifest(base_url, manifest_name)
                    
                    print("💾 Staging files...")
                    if ota.ota_update(base_url, manifest=manifest):
                        print("🎉 OTA Successful! Rebooting...")
                        config.update_config({"client": {"firmware_version": fw_ver}})
                        time.sleep(1)
                        import machine
                        machine.reset()
                except Exception as ota_err:
                    print("❌ OTA failed:", ota_err)
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
    if not _enabled or not _is_connected or _client is None:
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
    
    try:
        import scheduler
        scheduler.register_broadcast_callback(publish_hub_schedules)
    except Exception as reg_err:
        print("Failed to register schedules broadcast callback:", reg_err)
        
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
    config_topic = f"{site}/{group}/{client_type}/{client_id}/config"
    telemetry_interval = mqtt_cfg.get("telemetry_interval_sec", 30)
    
    last_telemetry_time = 0
    last_ping_time = 0
    
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
                    keepalive=mqtt_cfg.get("keepalive", 20)
                )
                
                # Configure Last Will and Testament (LWT) for abrupt disconnects in standard envelope
                lwt_payload = ujson.dumps({
                    "source": client_id,
                    "target": "backend_api",
                    "msg_type": "STATUS",
                    "timestamp": config.get_unix_time(),
                    "route": {
                        "transport": "MQTT",
                        "route_id": "lwt",
                        "current_hop_index": 0,
                        "hops": [client_id],
                        "link_diagnostics": []
                    },
                    "data": {
                        "device_id": client_id,
                        "status": "offline",
                        "reason": "keepalive_timeout"
                    }
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
                
                # Subscribe to hub command and configuration topics
                _client.subscribe(cmd_topic.encode('utf-8'))
                _client.subscribe(config_topic.encode('utf-8'))
                print(f" Subscribed to Hub topics: {cmd_topic}, {config_topic}")
                
                client_mode = cfg.get("client", {}).get("mode", "normal")
                is_pending_claim = (client_mode != "normal" and not _provision_confirmed)
                initial_status = "BLE_CLAIM_PENDING" if is_pending_claim else "online"
                
                # Publish startup status in standard envelope
                if site != "default_site":
                    publish_msg(status_topic, {
                        "source": client_id,
                        "target": "backend_api",
                        "msg_type": "STATUS",
                        "timestamp": config.get_unix_time(),
                        "route": {
                            "transport": "MQTT",
                            "route_id": "direct",
                            "current_hop_index": 0,
                            "hops": [client_id],
                            "link_diagnostics": []
                        },
                        "data": {
                            "device_id": client_id,
                            "status": initial_status,
                            "fw_ver": cfg.get("client", {}).get("firmware_version", "hub_v1.0.0")
                        }
                    }, retain=True)
                    print(f" Published startup status: {initial_status} to {status_topic}")
                else:
                    print("ERROR: 'site' not set in config. Cannot publish hub status.")
                
                # Check if device is pending provision confirmation
                global _timer_started
                if is_pending_claim and not _timer_started:
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
        
        now = time.time()

        # Send MQTT PINGREQ keepalive to broker to maintain active session and prevent LWT triggers
        if _is_connected and (now - last_ping_time > 15):
            try:
                _lock.acquire()
                try:
                    _client.ping()
                finally:
                    _lock.release()
                last_ping_time = now
            except Exception as ping_err:
                print("MQTT ping error (connection dropped):", ping_err)
                _is_connected = False

        # Periodically publish hub status telemetry ONLY when claim is completed (mode: normal)
        client_mode = cfg.get("client", {}).get("mode", "normal")
        is_normal_operating = (client_mode == "normal" or _provision_confirmed)
        if _is_connected and is_normal_operating and (now - last_telemetry_time > telemetry_interval):
            hub_status = cfg.get("client", {}).get("status", "Enabled")
            publish_hub_telemetry(hub_status)
            last_telemetry_time = now
            
        time.sleep(0.1)
