import ujson
import machine
import time
import config
import ubinascii
import led_status

try:
    import bluetooth
    has_ble = True
except ImportError:
    has_ble = False

is_ble_connected = False
_shutdown_requested = False
pending_config = None
ble_instance = None
write_handle = None
read_handle = None

_IRQ_CENTRAL_CONNECT = 1
_IRQ_CENTRAL_DISCONNECT = 2
_IRQ_GATTS_WRITE = 3

if has_ble:
    _SERVICE_UUID = bluetooth.UUID("0000ffe0-0000-1000-8000-00805f9b34fb")
    _WRITE_CHAR_UUID = bluetooth.UUID("0000ffe1-0000-1000-8000-00805f9b34fb")
    _READ_CHAR_UUID = bluetooth.UUID("0000ffe2-0000-1000-8000-00805f9b34fb")

    _WRITE_CHAR = (_WRITE_CHAR_UUID, bluetooth.FLAG_WRITE | bluetooth.FLAG_WRITE_NO_RESPONSE)
    _READ_CHAR = (_READ_CHAR_UUID, bluetooth.FLAG_READ)
    _SERVICE = (_SERVICE_UUID, (_WRITE_CHAR, _READ_CHAR))

def is_connected():
    return is_ble_connected

def get_advertising_payload(name="ESP32_Setup"):
    flags = b'\x02\x01\x06'
    name_bytes = name.encode('utf-8')
    name_field = bytes([len(name_bytes) + 1, 0x09]) + name_bytes
    # 16-bit Service UUID: ffe0 (0x03 indicates 16-bit Service Class UUIDs list)
    service_uuid_bytes = b'\xe0\xff'
    service_field = bytes([len(service_uuid_bytes) + 1, 0x03]) + service_uuid_bytes
    return flags + service_field + name_field

def start_advertising():
    global ble_instance
    cfg = config.load_config()
    client_id = cfg.get("client", {}).get("id", "esp32_node_01")
    dev_name = f"{client_id}_Setup"
    adv_payload = get_advertising_payload(dev_name)
    try:
        ble_instance.gap_advertise(None)
    except:
        pass
    ble_instance.gap_advertise(100000, adv_payload)
    print(f" BLE Advertising started: {dev_name}")

def irq_handler(event, data):
    global is_ble_connected, pending_config, ble_instance, write_handle
    if event == _IRQ_CENTRAL_CONNECT:
        conn_handle, addr_type, addr = data
        is_ble_connected = True
        print(" BLE Central connected:", ubinascii.hexlify(addr).decode())
        try:
            led_status.set_status("BLE_CONNECTED")
        except:
            pass
    elif event == _IRQ_CENTRAL_DISCONNECT:
        conn_handle, addr_type, addr = data
        is_ble_connected = False
        print(" BLE Central disconnected.")
        if not _shutdown_requested:
            try:
                led_status.set_status("BLE_PROVISIONING")
            except:
                pass
            try:
                start_advertising()
            except Exception:
                pass
    elif event == _IRQ_GATTS_WRITE:
        conn_handle, value_handle = data
        if value_handle == write_handle:
            pending_config = ble_instance.gatts_read(write_handle)

def update_device_config(data):
    cfg = config.load_config()
    
    # 1. WiFi parameters  support both key naming conventions
    ssid = data.get("ssid") or data.get("wifi_ssid")
    password = data.get("password") or data.get("wifi_pass", "")
    if ssid:
        cfg["wifi"] = {"networks": [{"ssid": ssid, "password": password}]}
        print(f" Wi-Fi Config updated: SSID={ssid}")
        
    if "mqtt_broker" in data:
        cfg.setdefault("mqtt", {})["server"] = data["mqtt_broker"]
        print(f" MQTT Broker updated: {data['mqtt_broker']}")
        
    # 2. Hub MAC / ID (Pump / Valve)
    hub_val = data.get("hub_mac") or data.get("hub_device_id")
    if hub_val:
        # Check if it is a formatted 6-byte MAC address (xx:xx:xx:xx:xx:xx)
        if isinstance(hub_val, str) and hub_val.count(':') == 5 and len(hub_val) == 17:
            cfg.setdefault("hub", {})["mac"] = hub_val
            print(f" Hub MAC updated: {hub_val}")
        else:
            cfg.setdefault("hub", {})["id"] = hub_val
            cfg.setdefault("hub", {})["mac"] = "00:00:00:00:00:00"
            print(f" Hub ID updated (will discover MAC via ESP-NOW): {hub_val}")

    # 3. Parent MAC (Valve)
    if "parent_mac" in data:
        parent_val = data["parent_mac"]
        if isinstance(parent_val, str) and parent_val.count(':') == 5 and len(parent_val) == 17:
            cfg.setdefault("parent", {})["mac"] = parent_val
            print(f" Parent MAC updated: {parent_val}")
        else:
            cfg.setdefault("parent", {})["id"] = parent_val
            cfg.setdefault("parent", {})["mac"] = "00:00:00:00:00:00"
            print(f" Parent ID updated: {parent_val}")
        
    # 4. Custom Name
    if "custom_name" in data:
        cfg.setdefault("client", {})["custom_name"] = data["custom_name"]
        print(f" Custom Name updated: {data['custom_name']}")

    # 4.5 Site / Location
    site = data.get("site") or data.get("location")
    if site:
        cfg.setdefault("client", {})["site"] = site
        print(f" Site updated: {site}")

    # 5. Controller/Board Type
    if "controller_type" in data or "board_type" in data:
        board_type = data.get("controller_type") or data.get("board_type")
        cfg.setdefault("client", {})["controller_type"] = board_type
        cfg.setdefault("client", {})["board_type"] = board_type
        print(f" Board/Controller Type updated: {board_type}")
        
    # 6. Node ID / Client ID  support both key naming conventions
    node_id = data.get("node_id") or data.get("client_id")
    if node_id:
        cfg.setdefault("client", {})["id"] = node_id
        cfg.setdefault("mqtt", {})["topic_prefix"] = f"farm/{node_id}"
        print(f" Node ID updated: {node_id}")

    # 7. Time initialization
    if "timestamp" in data:
        try:
            ts = data["timestamp"]
            # Convert Unix epoch (1970) to MicroPython epoch (2000)
            tm = time.gmtime(ts - 946684800)
            rtc = machine.RTC()
            rtc.datetime((tm[0], tm[1], tm[2], tm[6], tm[3], tm[4], tm[5], 0))
            print(f" RTC time initialized via BLE to: {time.localtime()}")
        except Exception as e:
            print(" Failed to initialize RTC time from BLE:", e)
        
    cfg.setdefault("client", {})["mode"] = "pending_confirm"
    
    config.save_config(cfg)
    print(" Configuration saved. Rebooting device to start MQTT connection & claim confirmation...")
    try:
        import sys
        sys.stdout.flush()
    except Exception:
        pass

def start_provisioning():
    global ble_instance, write_handle, read_handle, pending_config, _shutdown_requested
    if not has_ble:
        print(" Bluetooth (BLE) is not supported on this hardware platform.")
        return
    print(" Initializing Common BLE Provisioning Service...")
    _shutdown_requested = False
    try:
        import gc
        import network

        try:
            sta = network.WLAN(network.STA_IF)
            if sta.active():
                sta.active(False)
        except Exception:
            pass
        try:
            ap = network.WLAN(network.AP_IF)
            if ap.active():
                ap.active(False)
        except Exception:
            pass

        # Read factory MAC from eFuse via machine.unique_id without touching Wi-Fi subsystem
        mac_str = "000000000000"
        try:
            mac_str = ubinascii.hexlify(machine.unique_id()).decode()
        except:
            pass

        gc.collect()
        time.sleep_ms(100)

        for attempt in range(3):
            try:
                ble_instance = bluetooth.BLE()
                ble_instance.active(True)
                break
            except Exception as e:
                ble_instance = None
                if attempt == 2:
                    raise e
                print(f" BLE activation attempt {attempt + 1} failed: {e}. Retrying...")
                gc.collect()
                time.sleep_ms(500)

        ble_instance.irq(irq_handler)
        ((write_handle, read_handle),) = ble_instance.gatts_register_services((_SERVICE,))
        ble_instance.gatts_set_buffer(write_handle, 512, False)

        cfg = config.load_config()
        client_cfg = cfg.get("client", {})

        dev_info = {
            "device_id": client_cfg.get("id", "esp32_node_01"),
            "device_type": client_cfg.get("type", "unknown"),
            "controller_type": client_cfg.get("controller_type") or client_cfg.get("board_type", "ESP32-S3"),
            "board_type": client_cfg.get("board_type") or client_cfg.get("controller_type", "ESP32-S3"),
            "mac": mac_str,
            "fw_ver": client_cfg.get("firmware_version", "1.0.0")
        }
        ble_instance.gatts_write(read_handle, ujson.dumps(dev_info).encode('utf-8'))
        
        start_advertising()
    except Exception as e:
        import sys
        print(" BLE Provisioning initialization failed:")
        sys.print_exception(e)
        return

    # Polling loop
    while True:
        if pending_config is not None:
            raw_bytes = pending_config
            pending_config = None
            try:
                _shutdown_requested = True
                payload_str = raw_bytes.decode('utf-8').strip()
                print("BLE Received JSON configuration:", payload_str)
                provision_data = ujson.loads(payload_str)
                
                try:
                    ble_instance.gap_advertise(None)
                except:
                    pass

                update_device_config(provision_data)
                time.sleep(1)
                try:
                    if ble_instance is not None:
                        ble_instance.active(False)
                        ble_instance = None
                except Exception as ble_shutdown_err:
                    pass
                try:
                    import sys
                    sys.stdout.write("\r\n--- BLE PROVISIONING COMPLETE: SOFT REBOOTING ESP32 ---\r\n")
                    sys.stdout.flush()
                except Exception:
                    pass
                time.sleep_ms(300)
                machine.soft_reset()
            except Exception as e:
                print("Failed to parse/apply BLE config payload:", e)
        time.sleep(0.5)
