# main.py (Valve Controller)
import _thread
import time
import machine
import os
import gc
import config
import led_status
import ble_manager
import esp_now_client

# State
solenoid_open_pin = None
solenoid_close_pin = None
valve_state = "CLOSED" # "OPEN", "CLOSED"
last_telemetry_time = 0

def pulse_solenoid(open_pulse=True):
    global solenoid_open_pin, solenoid_close_pin, valve_state
    if solenoid_open_pin is None or solenoid_close_pin is None:
        return
        
    print(f"🚰 Solenoid Action: {'OPENING' if open_pulse else 'CLOSING'} pulse starting...")
    
    solenoid_open_pin.value(0)
    solenoid_close_pin.value(0)
    time.sleep_ms(10)
    
    if open_pulse:
        solenoid_open_pin.value(1)
        time.sleep_ms(100) # 100ms pulse
        solenoid_open_pin.value(0)
        valve_state = "OPEN"
        led_status.set_status("VALVE_OPEN")
    else:
        solenoid_close_pin.value(1)
        time.sleep_ms(100) # 100ms pulse
        solenoid_close_pin.value(0)
        valve_state = "CLOSED"
        led_status.set_status("VALVE_CLOSED")
        
    print(f"🚰 Solenoid Action complete. State: {valve_state}")

def handle_hub_commands(cmd, args):
    global valve_state
    print(f"⚙️ Handling local command: {cmd}")
    
    if cmd == "VALVE_OPEN":
        pulse_solenoid(open_pulse=True)
        esp_now_client.send_ack_or_tele_to_hub("ACK", {"valve_state": valve_state, "node_status": "active"})
        
    elif cmd == "VALVE_CLOSE":
        pulse_solenoid(open_pulse=False)
        esp_now_client.send_ack_or_tele_to_hub("ACK", {"valve_state": valve_state, "node_status": "active"})
        
    elif cmd == "GET_STATUS":
        esp_now_client.send_ack_or_tele_to_hub("ACK", {"valve_state": valve_state, "node_status": "active"})

def main():
    global solenoid_open_pin, solenoid_close_pin, valve_state, last_telemetry_time
    print("🚀 Valve Controller Starting...")
    
    # 1. Start Status LED
    _thread.start_new_thread(led_status.led_thread, ())
    
    # 2. Load configurations
    cfg_exists = "config.json" in os.listdir()
    cfg = None
    if cfg_exists:
        try:
            cfg = config.load_config()
        except Exception:
            cfg = None
            
    if cfg is None:
        print("⚠️ Configuration missing or corrupt! Falling back to BLE Provisioning Mode.")
        led_status.set_status("BLE_PROVISIONING")
        ble_manager.start_provisioning()
        return
        
    client_cfg = cfg.get("client", {})
    mode = client_cfg.get("mode", "ble_setup")
    
    if mode == "ble_setup":
        print("📡 BLE Setup mode configured. Initializing provisioning...")
        led_status.set_status("BLE_PROVISIONING")
        ble_manager.start_provisioning()
        return

    # 3. Start Normal Operations
    print("📶 Loading Valve Outputs...")
    led_status.set_status("VALVE_CLOSED")
    valve_state = "CLOSED"
    
    pins = cfg.get("valve", {}).get("pins", {})
    open_pin_num = pins.get("solenoid_open", 18)
    close_pin_num = pins.get("solenoid_close", 19)
    
    solenoid_open_pin = machine.Pin(open_pin_num, machine.Pin.OUT)
    solenoid_close_pin = machine.Pin(close_pin_num, machine.Pin.OUT)
    
    solenoid_open_pin.value(0)
    solenoid_close_pin.value(0)
    
    # Initialize ESP-NOW client
    esp_now_client.init_espnow_client()
    
    # Start receiver thread
    heartbeats = {"esp_now": time.time()}
    _thread.start_new_thread(esp_now_client.client_listen_loop, (heartbeats, handle_hub_commands))
    
    print("✅ Valve Node ready and running loop.")
    
    while True:
        try:
            gc.collect()
            
            # Send periodic telemetry if paired
            if esp_now_client.is_paired():
                now = time.time()
                if now - last_telemetry_time >= 10:
                    last_telemetry_time = now
                    esp_now_client.send_ack_or_tele_to_hub("TELE", {
                        "node_id": client_cfg.get("id"),
                        "status": "valve_idle",
                        "valve_state": valve_state,
                        "rssi": -50
                    })
                    
        except Exception as err:
            print("⚠️ Valve Loop Error:", err)
            
        time.sleep(1)

if __name__ == "__main__":
    main()
