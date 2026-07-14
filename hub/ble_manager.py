# ble_manager.py (Hub)
import bluetooth
import ujson
import machine
import time
import config
import ubinascii

is_ble_connected = False
pending_config = None
ble_instance = None
write_handle = None
read_handle = None

_IRQ_CENTRAL_CONNECT = 1
_IRQ_CENTRAL_DISCONNECT = 2
_IRQ_GATTS_WRITE = 3

_SERVICE_UUID = bluetooth.UUID("0000ffe0-0000-1000-8000-00805f9b34fb")
_WRITE_CHAR_UUID = bluetooth.UUID("0000ffe1-0000-1000-8000-00805f9b34fb")
_READ_CHAR_UUID = bluetooth.UUID("0000ffe2-0000-1000-8000-00805f9b34fb")

_WRITE_CHAR = (_WRITE_CHAR_UUID, bluetooth.FLAG_WRITE | bluetooth.FLAG_WRITE_NO_RESPONSE)
_READ_CHAR = (_READ_CHAR_UUID, bluetooth.FLAG_READ)
_SERVICE = (_SERVICE_UUID, (_WRITE_CHAR, _READ_CHAR))

def is_connected():
    global is_ble_connected
    return is_ble_connected

def get_advertising_payload(name="Hub_Setup"):
    flags = b'\x02\x01\x06'
    name_bytes = name.encode('utf-8')
    name_field = bytes([len(name_bytes) + 1, 0x09]) + name_bytes
    service_uuid_bytes = b'\xfb\x34\x9b\x5f\x80\x00\x00\x10\x00\x00\x00\x00\xe0\xff\x00\x00'
    service_field = bytes([len(service_uuid_bytes) + 1, 0x07]) + service_uuid_bytes
    return flags + service_field + name_field

def start_advertising():
    global ble_instance
    cfg = config.load_config()
    client_id = cfg.get("client", {}).get("id", "hub_master_01")
    dev_name = f"{client_id}_Setup"
    adv_payload = get_advertising_payload(dev_name)
    ble_instance.gap_advertise(100000, adv_payload)
    print(f"📣 BLE Advertising started: {dev_name}")

def irq_handler(event, data):
    global is_ble_connected, pending_config, ble_instance, write_handle
    if event == _IRQ_CENTRAL_CONNECT:
        conn_handle, addr_type, addr = data
        is_ble_connected = True
        print("🔗 BLE Central connected:", ubinascii.hexlify(addr).decode())
    elif event == _IRQ_CENTRAL_DISCONNECT:
        conn_handle, addr_type, addr = data
        is_ble_connected = False
        print("🔌 BLE Central disconnected.")
        start_advertising()
    elif event == _IRQ_GATTS_WRITE:
        conn_handle, value_handle = data
        if value_handle == write_handle:
            pending_config = ble_instance.gatts_read(write_handle)

def update_device_config(data):
    cfg = config.load_config()
    
    # WiFi network settings
    if "wifi_ssid" in data:
        ssid = data["wifi_ssid"]
        password = data.get("wifi_pass", "")
        cfg["wifi"] = {"networks": [{"ssid": ssid, "password": password}]}
        print(f"💾 Wi-Fi Config updated: SSID={ssid}")

    # MQTT broker settings
    if "mqtt_broker" in data:
        cfg["mqtt"]["server"] = data["mqtt_broker"]
        print(f"💾 MQTT Broker updated: {data['mqtt_broker']}")

    # Node Name / Custom Name
    if "custom_name" in data:
        cfg["client"]["custom_name"] = data["custom_name"]
        print(f"💾 Custom Name updated: {data['custom_name']}")

    # Node ID
    if "node_id" in data:
        cfg["client"]["id"] = data["node_id"]
        cfg["mqtt"]["topic_prefix"] = f"farm/{data['node_id']}"
        print(f"💾 Node ID updated: {data['node_id']}")

    # Change mode back to station (sta) mode
    cfg["client"]["mode"] = "sta"
    
    config.save_config(cfg)
    print("✅ Hub configuration saved. Rebooting device in 2 seconds...")

def start_provisioning():
    global ble_instance, write_handle, read_handle, pending_config
    print("🚀 Initializing BLE Provisioning Service on Hub...")
    try:
        ble_instance = bluetooth.BLE()
        
        # Ensure wifi active (required for clock/radio power on some ESP32 chips)
        import network
        wlan = network.WLAN(network.STA_IF)
        if not wlan.active():
            wlan.active(True)
            time.sleep_ms(200)

        for attempt in range(3):
            try:
                ble_instance.active(True)
                break
            except Exception as e:
                if attempt == 2:
                    raise e
                print(f"⚠️ BLE activation attempt {attempt + 1} failed: {e}. Retrying...")
                time.sleep_ms(500)

        ble_instance.irq(irq_handler)
        ((write_handle, read_handle),) = ble_instance.gatts_register_services((_SERVICE,))
        ble_instance.gatts_set_buffer(write_handle, 512, False)

        cfg = config.load_config()
        client_cfg = cfg.get("client", {})
        try:
            mac_bytes = wlan.config('mac')
            mac_str = ubinascii.hexlify(mac_bytes).decode()
        except:
            mac_str = "000000000000"

        dev_info = {
            "device_id": client_cfg.get("id", "hub_master_01"),
            "device_type": client_cfg.get("type", "hub"),
            "mac": mac_str,
            "fw_ver": client_cfg.get("firmware_version", "hub_v1.0.0")
        }
        ble_instance.gatts_write(read_handle, ujson.dumps(dev_info).encode('utf-8'))
        
        start_advertising()
    except Exception as e:
        print("🚨 BLE Provisioning initialization failed:", e)
        return

    # Polling loop
    while True:
        if pending_config is not None:
            raw_bytes = pending_config
            pending_config = None
            try:
                payload_str = raw_bytes.decode('utf-8').strip()
                print("📥 BLE Received JSON configuration:", payload_str)
                provision_data = ujson.loads(payload_str)
                
                try:
                    ble_instance.gap_advertise(None)
                except:
                    pass

                update_device_config(provision_data)
                time.sleep(2)
                machine.reset()
            except Exception as e:
                print("❌ Failed to parse/apply BLE config payload:", e)
        time.sleep(0.5)
