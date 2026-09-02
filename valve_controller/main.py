# main.py (Valve Controller)
import _thread
import time
import random
import machine
import os
import gc
import config
import led_status
import ble_manager
import espnow_client
import factory_reset

import espnow_ota

# State
valves = {}
last_telemetry_time = 0
next_telemetry_delay = 30

# Initialize OTA Receiver for ESP-NOW firmware updates
ota_receiver = espnow_ota.OTAReceiver(espnow_client.send_ack_or_tele_to_hub)

# Non-blocking command queue — receive thread enqueues, main loop executes
_cmd_queue = []

def save_valve_states():
    """Saves valve states to both RTC memory (for zero-wear deep sleep) and flash file."""
    try:
        import ujson
        states = {vid: v["state"] for vid, v in valves.items()}
        
        # 1. RTC Slow Memory (survives deep sleep, 0 flash wear)
        try:
            rtc = machine.RTC()
            rtc.memory(ujson.dumps(states))
        except Exception as rtc_err:
            pass
            
        # 2. Flash file (for cold boot / complete power loss recovery)
        with open("valve_states.json", "w") as f:
            ujson.dump(states, f)
    except Exception as e:
        print(" Failed to save valve states:", e)

def load_valve_states():
    """Loads valve states prioritizing RTC memory, falling back to flash."""
    # 1. Check RTC Slow Memory first
    try:
        import ujson
        rtc = machine.RTC()
        data = rtc.memory()
        if data:
            states = ujson.loads(data)
            if isinstance(states, dict) and states:
                print(" Restored valve states from RTC memory:", states)
                return states
    except Exception:
        pass

    # 2. Fallback to flash file
    try:
        import ujson
        import os
        if "valve_states.json" in os.listdir():
            with open("valve_states.json", "r") as f:
                states = ujson.load(f)
                print(" Restored valve states from flash storage:", states)
                return states
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

def handle_hub_commands(cmd_or_packet, args_or_sender=None):
    """
    Called from espnow_client listener thread when a message arrives from the Hub.
    Handles both packet dictionaries and (cmd, args) callbacks.
    """
    if isinstance(cmd_or_packet, dict):
        msg_type = cmd_or_packet.get("msg_type")
        data = cmd_or_packet.get("data", {})
        cmd = data.get("cmd") or data.get("command")
        sender_mac = args_or_sender or cmd_or_packet.get("source")
    else:
        cmd = cmd_or_packet
        data = args_or_sender if isinstance(args_or_sender, dict) else {}
        sender_mac = data.get("sender_mac")
        msg_type = "COMMAND" if cmd else "ACK"

    print(f" Received from Hub ({msg_type}): cmd={cmd}, data={data}")

    # Process ESP-NOW OTA packets directly for minimum latency
    if isinstance(data, dict) and (cmd == "OTA" or str(data.get("action", "")).startswith("OTA_")):
        led_status.set_status("BLE_PROVISIONING")
        ota_receiver.handle_packet(data, sender_mac)
        return

    if msg_type in ("CMD", "COMMAND"):
        _cmd_queue.append((cmd, data, sender_mac))
    elif msg_type == "ACK":
        status_val = data.get("status")
        if status_val == "sleep_ok":
            print(" Hub returned SLEEP_OK.")
        else:
            print(" Hub ACK:", status_val)

def execute_command(cmd, args, sender_mac=None):
    if not cmd:
        return
        
    print(f" Executing Hub command: {cmd} with args: {args}")
    
    if cmd == "SET_VALVES":
        # Target states can be passed as {"valves": {...}} or directly as {...}
        target_states = args.get("valves") if (isinstance(args, dict) and "valves" in args) else args
        
        if isinstance(target_states, dict):
            # Check for global ALL or '0' shortcut (e.g. {"ALL": "CLOSED"} or {"0": "OPEN"})
            global_action = target_states.get("ALL") or target_states.get("all") or target_states.get("0")
            if global_action:
                open_pulse = (str(global_action).upper() == "OPEN")
                for vid in valves.keys():
                    pulse_solenoid(vid, open_pulse=open_pulse)
            else:
                for vid, target_state in target_states.items():
                    if str(vid) in valves:
                        pulse_solenoid(str(vid), open_pulse=(str(target_state).upper() == "OPEN"))
        
        any_open = any(v.get("state") == "OPEN" for v in valves.values())
        espnow_client.send_ack_or_tele_to_hub("ACK", {
            "status": "VALVES_UPDATED",
            "valves": {vid: v["state"] for vid, v in valves.items()},
            "node_status": "watering" if any_open else "valve_idle"
        }, target_mac=sender_mac)

    elif cmd == "COM_TEST":
        print(" Visual COM_TEST triggered on Valve Controller!")
        try:
            prev_status = getattr(led_status, "_state", "VALVE_CLOSED")
            led_status.set_status("BLE_PROVISIONING")
            time.sleep(1.5)
            led_status.set_status(prev_status)
            any_open = any(v.get("state") == "OPEN" for v in valves.values())
            espnow_client.send_ack_or_tele_to_hub("ACK", {
                "status": "COM_TEST_OK",
                "cmd": "COM_TEST",
                "valves": {vid: v["state"] for vid, v in valves.items()},
                "node_status": "watering" if any_open else "valve_idle"
            }, target_mac=sender_mac)
        except Exception as e:
            print("COM_TEST error:", e)

    elif cmd == "GET_STATUS":
        any_open = any(v.get("state") == "OPEN" for v in valves.values())
        espnow_client.send_ack_or_tele_to_hub("ACK", {
            "valves": {vid: v["state"] for vid, v in valves.items()},
            "node_status": "watering" if any_open else "valve_idle"
        }, target_mac=sender_mac)

    elif cmd == "SET_CONFIG":
        print(" SET_CONFIG command received:", args)
        try:
            config_payload = args.get("config") or args.get("settings") or args
            if isinstance(config_payload, dict):
                clean_payload = {k: v for k, v in config_payload.items() if k not in ("cmd", "command", "target", "source", "msg_type")}
                config.update_config(clean_payload)
                print(" Configuration updated successfully on Valve Controller.")
            
            any_open = any(v.get("state") == "OPEN" for v in valves.values())
            espnow_client.send_ack_or_tele_to_hub("ACK", {
                "status": "CONFIG_UPDATED",
                "cmd": "SET_CONFIG",
                "valves": {vid: v["state"] for vid, v in valves.items()},
                "node_status": "watering" if any_open else "valve_idle"
            }, target_mac=sender_mac)
            
            if isinstance(args, dict) and args.get("reboot"):
                time.sleep_ms(300)
                machine.reset()
        except Exception as cfg_err:
            print(" SET_CONFIG failed:", cfg_err)

    elif cmd == "REBOOT":
        print(" REBOOT command received! Rebooting Valve Controller...")
        try:
            any_open = any(v.get("state") == "OPEN" for v in valves.values())
            espnow_client.send_ack_or_tele_to_hub("ACK", {
                "status": "REBOOTING",
                "cmd": "REBOOT",
                "valves": {vid: v["state"] for vid, v in valves.items()},
                "node_status": "watering" if any_open else "valve_idle"
            }, target_mac=sender_mac)
        except Exception:
            pass
        time.sleep_ms(300)
        machine.reset()

    elif cmd == "OTA":
        print(" OTA command received:", args)
        if isinstance(args, dict) and "action" in args:
            led_status.set_status("BLE_PROVISIONING")
            ota_receiver.handle_packet(args, sender_mac)
        else:
            # Fallback direct Wi-Fi OTA if full URL provided and Wi-Fi credentials exist
            try:
                import network_manager
                cfg = config.load_config()
                wifi_networks = cfg.get("wifi", {}).get("networks", [])
                if wifi_networks and network_manager.connect():
                    import ota
                    base_url = args.get("url") if isinstance(args, dict) else None
                    if not base_url:
                        base_url = cfg.get("ota", {}).get("base_url", "http://10.10.10.211:8000/fw")
                    manifest = ota.fetch_manifest(base_url)
                    if ota.ota_update(base_url, manifest=manifest):
                        time.sleep(1)
                        machine.reset()
                else:
                    espnow_client.send_ack_or_tele_to_hub("ACK", {
                        "status": "OTA_READY_FOR_ESPNOW",
                        "valves": {vid: v["state"] for vid, v in valves.items()},
                        "node_status": "active"
                    }, target_mac=sender_mac)
            except Exception as ota_err:
                print(" Direct Wi-Fi OTA failed:", ota_err)

def main():
    global last_telemetry_time, next_telemetry_delay
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

    # Load previously saved states from RTC memory or flash
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
    espnow_client.init_espnow_client()
    
    # Check if Deep Sleep is enabled
    deep_sleep_enabled = client_cfg.get("deep_sleep_enabled", True)
    deep_sleep_sec = int(client_cfg.get("deep_sleep_sec", 30))

    if deep_sleep_enabled:
        print(f" [Power Mode] Deep Sleep Enabled (Sleep interval: {deep_sleep_sec}s)")
        
        # Start background threads for fast message exchange
        heartbeats = {"esp_now": time.time()}
        _thread.start_new_thread(espnow_client.client_tx_loop, ())
        _thread.start_new_thread(espnow_client.client_listen_loop, (heartbeats, handle_hub_commands))
        
        # 1. Send Check-In / Telemetry to Hub
        any_open = any(v.get("state") == "OPEN" for v in valves.values())
        node_status = "watering" if any_open else "valve_idle"
        telemetry = {
            "status": node_status,
            "valves": {vid: v["state"] for vid, v in valves.items()},
            "rssi": -50,
            "sleep_sec": deep_sleep_sec
        }
        espnow_client.send_ack_or_tele_to_hub("TELE", telemetry)
        print(" Check-In Telemetry sent to Hub. Listening for commands...")

        # 2. Wait up to 600ms for incoming Hub response / mailbox commands
        start_wait = time.time()
        while time.time() - start_wait < 0.6:
            if _cmd_queue:
                cmd, args, sender_mac = _cmd_queue.pop(0)
                execute_command(cmd, args, sender_mac)
                time.sleep_ms(150)
                break
            time.sleep_ms(20)

        # 3. If an OTA update session is in progress, stay awake until complete!
        while ota_receiver.is_in_progress():
            if _cmd_queue:
                cmd, args, sender_mac = _cmd_queue.pop(0)
                execute_command(cmd, args, sender_mac)
            time.sleep_ms(50)

        # 4. Enter Deep Sleep
        print(f" Going to Deep Sleep for {deep_sleep_sec}s. Goodnight!")
        time.sleep_ms(50)
        try:
            espnow_client.stop_client()
        except:
            pass
        machine.deepsleep(deep_sleep_sec * 1000)

    else:
        # Continuous Running Loop (Non-sleep mode)
        print(" [Power Mode] Continuous Loop Active (Deep sleep disabled).")
        heartbeats = {"esp_now": time.time()}
        _thread.start_new_thread(espnow_client.client_tx_loop, ())
        _thread.start_new_thread(espnow_client.client_listen_loop, (heartbeats, handle_hub_commands))
        
        while True:
            try:
                gc.collect()

                # Drain command queue
                if _cmd_queue:
                    cmd, args, sender_mac = _cmd_queue.pop(0)
                    execute_command(cmd, args, sender_mac)

                # Send periodic telemetry
                if espnow_client.is_paired():
                    now = time.time()
                    if now - last_telemetry_time >= next_telemetry_delay:
                        last_telemetry_time = now
                        any_open = any(v.get("state") == "OPEN" for v in valves.values())
                        node_status = "watering" if any_open else "valve_idle"
                        telemetry = {
                            "status": node_status,
                            "valves": {vid: v["state"] for vid, v in valves.items()},
                            "rssi": -50
                        }
                        espnow_client.send_ack_or_tele_to_hub("TELE", telemetry)

                time.sleep_ms(100)

            except Exception as err:
                print(" Valve Loop Error:", err)
                time.sleep(1)
            
try:
    main()
except KeyboardInterrupt:
    print(" Keyboard interrupt received; stopping Valve Controller...")
    try:
        espnow_client.stop_client()
    except Exception as stop_err:
        pass
    print(" Valve Controller stopped")
except Exception as e:
    print(" Main loop error:", e)
