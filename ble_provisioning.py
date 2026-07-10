# ble_provisioning.py
import bluetooth
import ujson
import machine
import time
import config
import ubinascii
import _thread

# Global state
is_ble_connected = False
pending_config = None
ble_instance = None
write_handle = None
read_handle = None

# BLE IRQ Event Constants
_IRQ_CENTRAL_CONNECT = 1
_IRQ_CENTRAL_DISCONNECT = 2
_IRQ_GATTS_WRITE = 3

# Service and Characteristic UUIDs (using standard 128-bit format)
_SERVICE_UUID = bluetooth.UUID("0000ffe0-0000-1000-8000-00805f9b34fb")
_WRITE_CHAR_UUID = bluetooth.UUID("0000ffe1-0000-1000-8000-00805f9b34fb")
_READ_CHAR_UUID = bluetooth.UUID("0000ffe2-0000-1000-8000-00805f9b34fb")

_WRITE_CHAR = (_WRITE_CHAR_UUID, bluetooth.FLAG_WRITE | bluetooth.FLAG_WRITE_NO_RESPONSE)
_READ_CHAR = (_READ_CHAR_UUID, bluetooth.FLAG_READ)

_SERVICE = (_SERVICE_UUID, (_WRITE_CHAR, _READ_CHAR))

def is_connected():
    global is_ble_connected
    return is_ble_connected

def get_advertising_payload(name="ESP32_Setup"):
    flags = b'\x02\x01\x06' # BR/EDR Not Supported, General Discoverable
    
    # Complete Local Name
    name_bytes = name.encode('utf-8')
    name_field = bytes([len(name_bytes) + 1, 0x09]) + name_bytes
    
    # Complete List of 128-bit Service Class UUIDs
    service_uuid_bytes = b'\xfb\x34\x9b\x5f\x80\x00\x00\x10\x00\x00\x00\x00\xe0\xff\x00\x00'
    service_field = bytes([len(service_uuid_bytes) + 1, 0x07]) + service_uuid_bytes
    
    return flags + service_field + name_field

def start_advertising():
    global ble_instance
    cfg = config.load_config()
    client_id = cfg.get("client", {}).get("id", "esp32_pump_01")
    dev_name = f"{client_id}_Setup"
    
    adv_payload = get_advertising_payload(dev_name)
    # 100ms interval
    ble_instance.gap_advertise(100000, adv_payload)
    print(f"📣 BLE Advertising started for name: {dev_name}")

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
            # Read local GATT database value for this handle.
            pending_config = ble_instance.gatts_read(write_handle)

def update_device_config(data):
    cfg = config.load_config()
    
    # 1. Update Wi-Fi networks list
    if "ssid" in data:
        ssid = data["ssid"]
        password = data.get("password", "")
        cfg["wifi"] = {"networks": [{"ssid": ssid, "password": password}]}
        print(f"💾 Wi-Fi Config updated: SSID={ssid}")
        
    # 2. Update MQTT server
    if "mqtt_broker" in data:
        cfg["mqtt"]["server"] = data["mqtt_broker"]
        print(f"💾 MQTT Broker updated: Server={data['mqtt_broker']}")
        
    # 3. Update client ID
    if "client_id" in data:
        cfg["client"]["id"] = data["client_id"]
        print(f"💾 Client ID updated: ID={data['client_id']}")
        
    # 4. Set client mode back to sta
    cfg["client"]["mode"] = "sta"
    
    # Save the updated configuration
    config.save_config(cfg)
    print("✅ Configuration saved. Rebooting device in 2 seconds...")

def start_provisioning():
    global ble_instance, write_handle, read_handle, pending_config
    print("🚀 Initializing BLE Provisioning Service...")
    
    try:
        # Instantiate BLE
        ble_instance = bluetooth.BLE()
        ble_instance.active(True)
        ble_instance.irq(irq_handler)
        
        # Register GATT services
        ((write_handle, read_handle),) = ble_instance.gatts_register_services((_SERVICE,))
        
        # Set buffer size for provisioning writes to 512 bytes
        ble_instance.gatts_set_buffer(write_handle, 512, False)
        
        # Write device info to the read characteristic
        cfg = config.load_config()
        client_cfg = cfg.get("client", {})
        
        import network
        wlan = network.WLAN(network.STA_IF)
        try:
            mac_bytes = wlan.config('mac')
            mac_str = ubinascii.hexlify(mac_bytes).decode()
        except:
            mac_str = "000000000000"
            
        dev_info = {
            "device_id": client_cfg.get("id", "esp32_pump_01"),
            "device_type": client_cfg.get("type", "pump"),
            "serial_number": client_cfg.get("serial_number", "SN-UNKNOWN"),
            "model": client_cfg.get("model", "MODEL-UNKNOWN"),
            "mac": mac_str,
            "fw_ver": client_cfg.get("firmware_version", "firmesp32_v7")
        }
        ble_instance.gatts_write(read_handle, ujson.dumps(dev_info).encode('utf-8'))
        
        # Start advertising
        start_advertising()
        
    except Exception as e:
        print("🚨 BLE Provisioning initialization failed:", e)
        return
        
    # Main polling loop to process incoming data outside IRQ context
    while True:
        if pending_config is not None:
            raw_bytes = pending_config
            pending_config = None # Clear immediately
            
            try:
                payload_str = raw_bytes.decode('utf-8').strip()
                print("📥 BLE Received provisioning JSON payload:", payload_str)
                provision_data = ujson.loads(payload_str)
                
                # Turn off advertising
                try:
                    ble_instance.gap_advertise(None)
                except:
                    pass
                
                # Update config
                update_device_config(provision_data)
                
                # Audible feedback (Buzzer alarm)
                try:
                    buzzer_pin_num = cfg.get("pump", {}).get("pins", {}).get("buzzer", 21)
                    buzzer = machine.Pin(buzzer_pin_num, machine.Pin.OUT)
                    # Beep 3 times quickly to indicate success
                    for _ in range(3):
                        buzzer.value(1)
                        time.sleep(0.1)
                        buzzer.value(0)
                        time.sleep(0.1)
                except:
                    pass
                
                # LED feedback (flash run/fault sequence)
                try:
                    led_run_num = cfg.get("pump", {}).get("pins", {}).get("led_run", 2)
                    led = machine.Pin(led_run_num, machine.Pin.OUT)
                    for _ in range(10):
                        led.value(not led.value())
                        time.sleep(0.05)
                    led.value(0)
                except:
                    pass
                    
                time.sleep(1.5)
                machine.reset()
                
            except Exception as e:
                print("❌ Failed to parse/apply provisioning payload:", e)
                
        time.sleep(0.5)
