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
valves = {}
last_telemetry_time = 0

def update_valve_leds(valve_id):
    valve = valves.get(valve_id)
    if not valve:
        return
    state = valve.get("state", "CLOSED")
    
    led_on = valve.get("led_on_pin")
    led_off = valve.get("led_off_pin")
    led_fault = valve.get("led_fault_pin")
    
    # RGB/Multicolor LED status logic
    if state == "FAULT":
        if led_on: led_on.value(0)
        if led_off: led_off.value(0)
        if led_fault: led_fault.value(1) # Shines Blue/Fault color
    elif state == "OPEN":
        if led_on: led_on.value(1)      # Shines Green/ON color
        if led_off: led_off.value(0)
        if led_fault: led_fault.value(0)
    else: # CLOSED
        if led_on: led_on.value(0)
        if led_off: led_off.value(1)     # Shines Red/OFF color
        if led_fault: led_fault.value(0)

def pulse_solenoid(valve_id="1", open_pulse=True):
    valve = valves.get(valve_id)
    if not valve:
        print(f"🚰 Valve {valve_id} not found!")
        return
        
    open_pin = valve.get("solenoid_open_pin")
    close_pin = valve.get("solenoid_close_pin")
    if open_pin is None or close_pin is None:
        print(f"🚰 Valve {valve_id} pins not configured!")
        return
        
    print(f"🚰 Solenoid Valve {valve_id} Action: {'OPENING' if open_pulse else 'CLOSING'} pulse starting...")
    
    open_pin.value(0)
    close_pin.value(0)
    time.sleep_ms(10)
    
    if open_pulse:
        open_pin.value(1)
        time.sleep_ms(100) # 100ms pulse
        open_pin.value(0)
        valve["state"] = "OPEN"
    else:
        close_pin.value(1)
        time.sleep_ms(100) # 100ms pulse
        close_pin.value(0)
        valve["state"] = "CLOSED"
        
    update_valve_leds(valve_id)
    print(f"🚰 Solenoid Valve {valve_id} Action complete. State: {valve['state']}")

def handle_hub_commands(cmd, args):
    print(f"⚙️ Handling local command: {cmd} with args: {args}")
    
    valve_id = "1"
    sender_mac = None
    if isinstance(args, dict):
        valve_id = str(args.get("valve_id", "1"))
        sender_mac = args.get("sender_mac")
    elif isinstance(args, str):
        try:
            import ujson
            parsed = ujson.loads(args)
            valve_id = str(parsed.get("valve_id", "1"))
            sender_mac = parsed.get("sender_mac")
        except:
            pass
            
    if cmd == "VALVE_OPEN":
        pulse_solenoid(valve_id, open_pulse=True)
        esp_now_client.send_ack_or_tele_to_hub("ACK", {
            "valve_id": valve_id,
            "valve_state": valves.get(valve_id, {}).get("state", "CLOSED"),
            "node_status": "active"
        }, target_mac=sender_mac)
        
    elif cmd == "VALVE_CLOSE":
        pulse_solenoid(valve_id, open_pulse=False)
        esp_now_client.send_ack_or_tele_to_hub("ACK", {
            "valve_id": valve_id,
            "valve_state": valves.get(valve_id, {}).get("state", "CLOSED"),
            "node_status": "active"
        }, target_mac=sender_mac)
        
    elif cmd == "GET_STATUS":
        esp_now_client.send_ack_or_tele_to_hub("ACK", {
            "valves": {vid: v["state"] for vid, v in valves.items()},
            "node_status": "active"
        }, target_mac=sender_mac)

def main():
    global last_telemetry_time
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
    
    valves_cfg = cfg.get("valves", [])
    valves_map = {}
    if isinstance(valves_cfg, list) and len(valves_cfg) > 0:
        valves_map = valves_cfg[0]
    elif isinstance(valves_cfg, dict):
        valves_map = valves_cfg


    # Initialize all valves
    for vid, pins in valves_map.items():
        open_pin_num = pins.get("solenoid_open")
        close_pin_num = pins.get("solenoid_close")
        led_on_num = pins.get("led_on")
        led_off_num = pins.get("led_off")
        led_fault_num = pins.get("led_fault")
        
        # Solenoids
        open_pin = machine.Pin(open_pin_num, machine.Pin.OUT) if open_pin_num is not None else None
        close_pin = machine.Pin(close_pin_num, machine.Pin.OUT) if close_pin_num is not None else None
        
        # LEDs
        led_on_pin = machine.Pin(led_on_num, machine.Pin.OUT) if led_on_num is not None else None
        led_off_pin = machine.Pin(led_off_num, machine.Pin.OUT) if led_off_num is not None else None
        led_fault_pin = machine.Pin(led_fault_num, machine.Pin.OUT) if led_fault_num is not None else None
        
        if open_pin: open_pin.value(0)
        if close_pin: close_pin.value(0)
        
        valves[str(vid)] = {
            "state": "CLOSED",
            "solenoid_open_pin": open_pin,
            "solenoid_close_pin": close_pin,
            "led_on_pin": led_on_pin,
            "led_off_pin": led_off_pin,
            "led_fault_pin": led_fault_pin
        }
        
        # Initialize LEDs status
        update_valve_leds(str(vid))
    
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
                        "valve_state": valves.get("1", {}).get("state", "CLOSED") if len(valves) == 1 else "multi",
                        "valves": {vid: v["state"] for vid, v in valves.items()},
                        "rssi": -50
                    })
                    
        except Exception as err:
            print("⚠️ Valve Loop Error:", err)
            
        time.sleep(1)

if __name__ == "__main__":
    main()
