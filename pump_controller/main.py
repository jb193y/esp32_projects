# main.py (Pump Controller)
import _thread
import time
import machine
import os
import gc
import config
import led_status
import ble_manager
import sensors
import safety_monitor
import esp_now_client

# State
pump_relay_pin = None
active_faults = []
current_mode = "OFF" # "OFF", "RUNNING", "FAULT", "RESTART_DELAY"
restart_allowed_at = 0
last_telemetry_time = 0

def buzzer_beep(count=1, duration_ms=100):
    cfg = config.load_config()
    buzzer_pin_num = cfg.get("pump", {}).get("pins", {}).get("buzzer", 21)
    try:
        buzzer = machine.Pin(buzzer_pin_num, machine.Pin.OUT)
        for _ in range(count):
            buzzer.value(1)
            time.sleep_ms(duration_ms)
            buzzer.value(0)
            time.sleep_ms(duration_ms)
    except:
        pass

def handle_estop_alert(reason):
    """
    Called by safety_monitor IRQ context via machine.schedule
    Runs in the main thread execution context.
    """
    global current_mode, active_faults
    current_mode = "FAULT"
    led_status.set_status("FAULT")
    if "E-STOP" not in active_faults:
        active_faults.append("E-STOP")
        
    print(f"🚨 Dispatching high-priority E-Stop alert to Hub: {reason}")
    esp_now_client.send_to_hub("ALERT", {
        "alert_type": "e_stop_tripped",
        "message": reason
    })
    buzzer_beep(3, 300)

def handle_hub_commands(cmd, args):
    """
    Callback when an ESP-NOW command arrives from the Hub
    """
    global pump_relay_pin, current_mode, active_faults, restart_allowed_at
    print(f"⚙️ Hub Command Received: {cmd}")
    
    if cmd == "PUMP_ON":
        if "E-STOP" in active_faults or not safety_monitor.check_safety_state():
            print("❌ Cannot start: E-Stop is active!")
            esp_now_client.send_to_hub("ACK", {"status": "rejected", "reason": "estop_active"})
            return
            
        if active_faults:
            print(f"❌ Cannot start: Active faults present: {active_faults}")
            esp_now_client.send_to_hub("ACK", {"status": "rejected", "reason": f"faults_{active_faults}"})
            return
            
        if current_mode == "RESTART_DELAY" and time.time() < restart_allowed_at:
            remaining = int(restart_allowed_at - time.time())
            print(f"❌ Cannot start: In restart delay. Remaining: {remaining}s")
            esp_now_client.send_to_hub("ACK", {"status": "rejected", "reason": f"restart_delay_{remaining}s"})
            return
            
        print("🔌 Turning Pump Relay ON")
        pump_relay_pin.value(1)
        current_mode = "RUNNING"
        led_status.set_status("RUNNING")
        buzzer_beep(1, 500)
        esp_now_client.send_to_hub("ACK", {"status": "pump_on"})
        
    elif cmd == "PUMP_OFF":
        print("🔌 Turning Pump Relay OFF")
        pump_relay_pin.value(0)
        if current_mode != "FAULT":
            current_mode = "OFF"
            led_status.set_status("NORMAL_OFF")
        esp_now_client.send_to_hub("ACK", {"status": "pump_off"})
        
    elif cmd == "CLEAR_FAULT":
        print("🔓 Fault clear request received")
        if "E-STOP" in active_faults:
            if not safety_monitor.reset_estop():
                esp_now_client.send_to_hub("ACK", {"status": "rejected", "reason": "estop_still_pressed"})
                return
        active_faults.clear()
        current_mode = "OFF"
        led_status.set_status("NORMAL_OFF")
        esp_now_client.send_to_hub("ACK", {"status": "faults_cleared"})

def check_sensors_and_safety():
    global current_mode, active_faults, pump_relay_pin, restart_allowed_at
    
    cfg = config.load_config()
    limits = cfg.get("safety", {})
    
    rms_current = sensors.read_rms_current()
    rms_voltage = sensors.read_rms_voltage()
    
    if not safety_monitor.check_safety_state():
        if "E-STOP" not in active_faults:
            active_faults.append("E-STOP")
        current_mode = "FAULT"
        led_status.set_status("FAULT")
        pump_relay_pin.value(0)
        return rms_current, rms_voltage
        
    if current_mode == "FAULT":
        pump_relay_pin.value(0)
        return rms_current, rms_voltage

    # 1. Overcurrent Safety Check
    if rms_current > limits.get("overcurrent_limit_amps", 15.0):
        print(f"🚨 Safety Tripped: Overcurrent ({rms_current}A > {limits.get('overcurrent_limit_amps')}A)!")
        active_faults.append("OVERCURRENT")
        current_mode = "FAULT"
        led_status.set_status("FAULT")
        pump_relay_pin.value(0)
        buzzer_beep(5, 100)
        esp_now_client.send_to_hub("ALERT", {"alert_type": "overcurrent_fault", "val": rms_current})
        return rms_current, rms_voltage

    # 2. Dry Run Safety Check
    if current_mode == "RUNNING" and rms_current < limits.get("dryrun_limit_amps", 1.5):
        print(f"🚨 Safety Tripped: Dry Run / Undercurrent ({rms_current}A < {limits.get('dryrun_limit_amps')}A)!")
        active_faults.append("DRY_RUN")
        current_mode = "FAULT"
        led_status.set_status("FAULT")
        pump_relay_pin.value(0)
        buzzer_beep(4, 150)
        esp_now_client.send_to_hub("ALERT", {"alert_type": "dry_run_fault", "val": rms_current})
        return rms_current, rms_voltage

    # 3. Overvoltage / Undervoltage Safety Check
    max_v = limits.get("overvoltage_limit_volts", 250.0)
    min_v = limits.get("undervoltage_limit_volts", 180.0)
    if rms_voltage > max_v or rms_voltage < min_v:
        print(f"⚠️ Voltage unstable ({rms_voltage}V outside [{min_v}, {max_v}])!")
        pump_relay_pin.value(0)
        current_mode = "RESTART_DELAY"
        led_status.set_status("RESTART_DELAY")
        restart_allowed_at = time.time() + limits.get("restart_delay_sec", 60)
        esp_now_client.send_to_hub("ALERT", {"alert_type": "voltage_instability_tripped", "val": rms_voltage})

    return rms_current, rms_voltage

def main():
    global pump_relay_pin, current_mode, last_telemetry_time
    print("🚀 Pump Controller Starting...")
    
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

    # 3. Start Normal Operations (STA Mode)
    print("📶 Loading sensors and safety monitors...")
    led_status.set_status("NORMAL_OFF")
    current_mode = "OFF"
    
    pins = cfg.get("pump", {}).get("pins", {})
    relay_pin_num = pins.get("relay", 16)
    pump_relay_pin = machine.Pin(relay_pin_num, machine.Pin.OUT)
    pump_relay_pin.value(0)
    
    sensors.init_sensors()
    safety_monitor.init_safety(pump_relay_pin, handle_estop_alert)
    
    esp_now_client.init_espnow_client(handle_hub_commands)
    
    heartbeats = {"esp_now": time.time()}
    _thread.start_new_thread(esp_now_client.client_listen_loop, (heartbeats, handle_hub_commands))
    
    print("✅ Pump systems ready and listening!")
    buzzer_beep(2, 80)
    
    while True:
        try:
            gc.collect()
            current, voltage = check_sensors_and_safety()
            
            now = time.time()
            if now - last_telemetry_time >= 5:
                last_telemetry_time = now
                
                status_code = current_mode
                if current_mode == "RESTART_DELAY":
                    status_code = f"RESTART_DELAY_{int(restart_allowed_at - now)}s"
                    
                esp_now_client.send_to_hub("TELE", {
                    "node_id": client_cfg.get("id"),
                    "status": status_code,
                    "rms_current": current,
                    "rms_voltage": voltage,
                    "active_faults": active_faults
                })
                
        except Exception as e:
            print("⚠️ Main loop error:", e)
            
        time.sleep(1)

if __name__ == "__main__":
    main()
