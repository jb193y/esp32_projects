# main.py (Hub)
import _thread
import time
import machine
import os
import gc
import config
import led_status
import ble_manager
import network_manager
import mqtt_client
import esp_now_master
import scheduler
import factory_reset

ESPNOW_ONLY_TEST = False

heartbeats = {
    "network": time.time(),
    "mqtt": time.time(),
    "esp_now": time.time(),
    "scheduler": time.time()
}

# Watchdog monitoring is now handled directly in the main execution loop to save thread overhead.

def main():
    print("Hub Master Controller Loading...")
    
    # Set default thread stack size to 8KB before starting any thread to prevent stack overflow
    try:
        _thread.stack_size(8192)
        print("Default thread stack size configured to 8KB")
    except Exception as ex:
        print("Failed to configure thread stack size:", ex)
    
    # 1. Start Status LED
    _thread.start_new_thread(led_status.led_thread, ())
    time.sleep(0.5)
    
    # 1.5 Start Factory Reset monitor
    factory_reset.start()
    
    # 2. Check if device is configured
    cfg_exists = "config.json" in os.listdir()
    cfg = None
    if cfg_exists:
        try:
            cfg = config.load_config()
        except Exception:
            cfg = None
            
    if cfg is None:
        print("Configuration missing or corrupt! Falling back to BLE Provisioning Mode.")
        led_status.set_status("BLE_PROVISIONING")
        ble_manager.start_provisioning()
        return
        
    client_cfg = cfg.get("client", {})
    mode = client_cfg.get("mode", "ble_setup")
    espnow_only = ESPNOW_ONLY_TEST or client_cfg.get("espnow_only", False)
    
    if mode == "ble_setup":
        print("BLE Setup mode configured. Initializing provisioning...")
        led_status.set_status("BLE_PROVISIONING")
        ble_manager.start_provisioning()
        return
        
    # 3. Start Normal Operations (STA Mode)
    print("Booting into STA normal operations...")
    led_status.set_status("WIFI_CONNECTING")
    
    # Launch only the services needed for the ESP-NOW transport test.
    if espnow_only:
        print("ESP-NOW-only test mode: WAN, MQTT, and scheduler disabled")
        heartbeats.pop("network", None)
        heartbeats.pop("mqtt", None)
        heartbeats.pop("scheduler", None)
        mqtt_client.set_enabled(False)
    else:
        mqtt_client.register_cmd_dispatcher(esp_now_master.dispatch_command_from_mqtt)
        _thread.start_new_thread(network_manager.wan_thread, (heartbeats,))
        _thread.start_new_thread(mqtt_client.mqtt_thread, (heartbeats,))

    receiver_fn = (esp_now_master.espnow_test_receiver_thread
                   if espnow_only else esp_now_master.espnow_receiver_thread)
    _thread.start_new_thread(receiver_fn, (heartbeats,))
    if not espnow_only:
        _thread.start_new_thread(scheduler.scheduler_thread, (heartbeats, esp_now_master.send_espnow_msg))
    print("All Hub systems active!")
    
    last_checked_system_time = time.time()
    while True:
        gc.collect()
        time.sleep(10)
        
        now = time.time()
        # Detect if system clock jumped (e.g. via NTP sync)
        time_diff = now - last_checked_system_time
        if abs(time_diff) > 100:
            print(f"Watchdog: System clock jump detected (diff={time_diff}s). Adjusting heartbeats.")
            for name in heartbeats:
                heartbeats[name] = now

        last_checked_system_time = now

        for name, last_time in heartbeats.items():
            age = now - last_time
            if age > 90:
                print(f"Watchdog: Thread '{name}' stalled ({age}s ago). Rebooting...")
                time.sleep(1)
                machine.reset()

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("Main loop error:", e)
