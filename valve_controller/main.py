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
import factory_reset

# State
valves = {}
last_telemetry_time = 0

# Non-blocking command queue — receive thread enqueues, main loop executes
_cmd_queue = []

def save_valve_states():
    try:
        import ujson
        states = {vid: v["state"] for vid, v in valves.items()}
        with open("valve_states.json", "w") as f:
            ujson.dump(states, f)
    except Exception as e:
        print(" Failed to save valve states:", e)

def load_valve_states():
    try:
        import ujson
        import os
        if "valve_states.json" in os.listdir():
            with open("valve_states.json", "r") as f:
                return ujson.load(f)
    except Exception as e:
        print(" Failed to load valve states:", e)
    return {}

def update_valve_leds(valve_id):
    valve = valves.get(valve_id)
    if not valve:
        return
    state = valve.get("state", "CLOSED")
    
    status_led = valve.get("status_led_pin")
    
    if status_led:
        if state == "OPEN":
            status_led.value(1)
        elif state == "FAULT":
            status_led.value(1)
        else: # CLOSED
            status_led.value(0)

    # Update main system status LED based on overall node state
    any_fault = any(v.get("state") == "FAULT" for v in valves.values())
    any_open = any(v.get("state") == "OPEN" for v in valves.values())
    
    if any_fault:
        led_status.set_status("FAULT")
    elif any_open:
        led_status.set_status("VALVE_OPEN")
    else:
        led_status.set_status("VALVE_CLOSED")

def pulse_solenoid(valve_id="1", open_pulse=True):
    valve = valves.get(valve_id)
    if not valve:
        print(f" Valve {valve_id} not found!")
        return
        
    open_pin = valve.get("solenoid_open_pin")
    close_pin = valve.get("solenoid_close_pin")
    if open_pin is None or close_pin is None:
        print(f" Valve {valve_id} pins not configured!")
        return
        
    print(f" Solenoid Valve {valve_id} Action: {'OPENING' if open_pulse else 'CLOSING'} pulse starting...")
    
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
    save_valve_states()
    print(f" Solenoid Valve {valve_id} Action complete. State: {valve['state']}")

def handle_hub_commands(cmd, args):
    # Non-blocking: just enqueue the command for the main loop to execute.
    # This keeps the ESP-NOW receive thread free to accept the next packet
    # immediately, preventing dropped commands during solenoid pulses.
    print(f" Queuing command: {cmd} with args: {args}")
    _cmd_queue.append((cmd, args))


def execute_command(cmd, args):
    """Called from the main loop — safe to block here."""
    print(f" Executing command: {cmd} with args: {args}")

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
            "valves": {vid: v["state"] for vid, v in valves.items()},
            "node_status": "active"
        }, target_mac=sender_mac)

    elif cmd == "VALVE_CLOSE":
        pulse_solenoid(valve_id, open_pulse=False)
        esp_now_client.send_ack_or_tele_to_hub("ACK", {
            "valve_id": valve_id,
            "valves": {vid: v["state"] for vid, v in valves.items()},
            "node_status": "active"
        }, target_mac=sender_mac)

    elif cmd == "VALVE_ENABLE":
        valve = valves.get(valve_id)
        if valve:
            valve["enabled"] = True
            save_valve_states()
        esp_now_client.send_ack_or_tele_to_hub("ACK", {
            "valve_id": valve_id,
            "status": "ENABLED",
            "valves": {vid: v["state"] for vid, v in valves.items()},
            "node_status": "active"
        }, target_mac=sender_mac)

    elif cmd == "VALVE_DISABLE":
        valve = valves.get(valve_id)
        if valve:
            valve["enabled"] = False
            if valve.get("state") == "OPEN":
                pulse_solenoid(valve_id, open_pulse=False)
            valve["state"] = "DISABLED"
            save_valve_states()
        esp_now_client.send_ack_or_tele_to_hub("ACK", {
            "valve_id": valve_id,
            "status": "DISABLED",
            "valves": {vid: v["state"] for vid, v in valves.items()},
            "node_status": "active"
        }, target_mac=sender_mac)

    elif cmd == "GET_STATUS":
        esp_now_client.send_ack_or_tele_to_hub("ACK", {
            "valves": {vid: v["state"] for vid, v in valves.items()},
            "node_status": "active"
        }, target_mac=sender_mac)

    elif cmd in ("BLINK_LED", "COM_TEST"):
        print("Visual COM_TEST / BLINK_LED triggered on Valve Controller!")
        def _blink_valve_bg():
            try:
                prev_status = getattr(led_status, "_state", "VALVE_CLOSED")
                led_status.set_status("BLE_PROVISIONING")
                time.sleep(3)
                led_status.set_status(prev_status)
                esp_now_client.send_ack_or_tele_to_hub("ACK", {
                    "status": "BLINK_COMPLETE",
                    "cmd": cmd,
                    "valves": {vid: v["state"] for vid, v in valves.items()},
                    "node_status": "active"
                }, target_mac=sender_mac)
            except Exception as e:
                print("Blink LED error:", e)
        _thread.start_new_thread(_blink_valve_bg, ())

    elif cmd == "OTA":
        print("🚀 OTA command received! Initiating firmware update...")
        try:
            try:
                esp_now_client._e.active(False)
            except:
                pass
                
            import network_manager
            cfg = config.load_config()
            wifi_networks = cfg.get("wifi", {}).get("networks", [])
            if not wifi_networks:
                raise Exception("No Wi-Fi credentials in config.json")
                
            print("📡 Connecting to Wi-Fi...")
            if not network_manager.connect():
                raise Exception("Failed to connect to Wi-Fi")
                
            import ota
            client_info = cfg.get("client", {})
            ota_cfg = cfg.get("ota", {})
            
            args_dict = {}
            if isinstance(args, dict):
                args_dict = args
            elif isinstance(args, str):
                try:
                    import ujson
                    args_dict = ujson.loads(args)
                except:
                    pass
                    
            ota_url = args_dict.get("url") or ota_cfg.get("base_url") or "http://10.10.10.211:8000/fw"
            manifest_name = args_dict.get("manifest_name") or ota_cfg.get("manifest") or "manifest.json"
            
            client_type = client_info.get("type", "valve").lower()
            hw_ver = client_info.get("hardware_version", "esp32_1.0")
            fw_ver = args_dict.get("version") or client_info.get("firmware_version", "valve_v1.0.0")
            
            base_url = f"{ota_url.rstrip('/')}/{client_type}/{hw_ver}/{fw_ver}"
            
            print(f"📡 Downloading manifest from: {base_url}/{manifest_name}")
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
            import machine
            machine.reset()

def main():
    global last_telemetry_time
    print(" Valve Controller Starting...")
    
    # 1. Start Status LED
    _thread.start_new_thread(led_status.led_thread, ())
    time.sleep(0.2)
    factory_reset.start()
    
    # 2. Load configurations
    cfg_exists = "config.json" in os.listdir()
    cfg = None
    if cfg_exists:
        try:
            cfg = config.load_config()
        except Exception:
            cfg = None
            
    if cfg is None:
        print(" Configuration missing or corrupt! Falling back to BLE Provisioning Mode.")
        led_status.set_status("BLE_PROVISIONING")
        ble_manager.start_provisioning()
        return
        
    client_cfg = cfg.get("client", {})
    mode = client_cfg.get("mode", "ble_setup")
    
    if mode == "ble_setup":
        print(" BLE Setup mode configured. Initializing provisioning...")
        led_status.set_status("BLE_PROVISIONING")
        ble_manager.start_provisioning()
        return

    # 3. Start Normal Operations
    print(" Loading Valve Outputs...")
    led_status.set_status("VALVE_CLOSED")
    
    valves_cfg = cfg.get("valves", [])
    valves_map = {}
    if isinstance(valves_cfg, list) and len(valves_cfg) > 0:
        valves_map = valves_cfg[0]
    elif isinstance(valves_cfg, dict):
        valves_map = valves_cfg


    # Load previously saved states to prevent out-of-sync states on reboot
    saved_states = load_valve_states()

    # Initialize all valves
    for vid, pins in valves_map.items():
        open_pin_num = pins.get("solenoid_open")
        close_pin_num = pins.get("solenoid_close")
        status_led_num = pins.get("status_led")
        
        # Solenoids
        open_pin = machine.Pin(open_pin_num, machine.Pin.OUT) if open_pin_num is not None else None
        close_pin = machine.Pin(close_pin_num, machine.Pin.OUT) if close_pin_num is not None else None
        
        # LED
        status_led_pin = machine.Pin(status_led_num, machine.Pin.OUT) if status_led_num is not None else None
        
        if open_pin: open_pin.value(0)
        if close_pin: close_pin.value(0)
        
        valves[str(vid)] = {
            "state": saved_states.get(str(vid), "CLOSED"),
            "solenoid_open_pin": open_pin,
            "solenoid_close_pin": close_pin,
            "status_led_pin": status_led_pin
        }
        
        # Initialize LEDs status
        update_valve_leds(str(vid))
    
    # Initialize ESP-NOW client
    esp_now_client.init_espnow_client()
    
    # Start receiver thread
    heartbeats = {"esp_now": time.time()}
    _thread.start_new_thread(esp_now_client.client_listen_loop, (heartbeats, handle_hub_commands))
    
    print(" Valve Node ready and running loop.")
    
    while True:
        try:
            gc.collect()

            # Drain command queue — execute one command per loop tick
            if _cmd_queue:
                cmd, args = _cmd_queue.pop(0)
                execute_command(cmd, args)

            # Send periodic telemetry if paired
            if esp_now_client.is_paired():
                now = time.time()
                if now - last_telemetry_time >= 10:
                    last_telemetry_time = now
                    any_open = any(v.get("state") == "OPEN" for v in valves.values())
                    node_status = "watering" if any_open else "valve_idle"
                    esp_now_client.send_ack_or_tele_to_hub("TELE", {
                        "node_id": client_cfg.get("id"),
                        "status": node_status,
                        "valves": {vid: v["state"] for vid, v in valves.items()},
                        "rssi": -50
                    })

        except Exception as err:
            print(" Valve Loop Error:", err)
            
try:
    main()
except Exception as e:
    print(" Main loop error:", e)
