# main.py (Hub)
import _thread
import time
import machine
import os
import gc
import config
import led_status
import ble_manager
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
    gc.collect()

    # 2. Configure default thread stack size to 4KB (saves ~30KB RAM over 8KB)
    try:
        _thread.stack_size(4096)
        print("Default thread stack size configured to 4KB")
    except Exception as ex:
        print("Failed to configure thread stack size:", ex)
    
    # 3. Start Status LED
    _thread.start_new_thread(led_status.led_thread, ())
    time.sleep_ms(200)
    
    # 4. Start Factory Reset monitor (runs on hardware Timer with 0 thread overhead)
    factory_reset.start()
    
    # 5. Check if device is configured
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
        
    # 6. Start Normal Operations (STA Mode)
    print("Booting into STA normal operations...")
    import network_manager
    import mqtt_client
    import espnow_master
    import scheduler

    led_status.set_status("WIFI_CONNECTING")
    gc.collect()
    
    if espnow_only:
        print("ESP-NOW-only test mode: WAN, MQTT, and scheduler disabled")
        heartbeats.pop("network", None)
        heartbeats.pop("mqtt", None)
        heartbeats.pop("scheduler", None)
        mqtt_client.set_enabled(False)
        receiver_fn = espnow_master.espnow_test_receiver_thread
        _thread.start_new_thread(receiver_fn, (heartbeats,))
    else:
        mqtt_client.register_cmd_dispatcher(espnow_master.dispatch_command_from_mqtt)
        
        # 6.1 Start WAN / Wi-Fi thread first so WPA2 AES handshake completes without memory contention
        _thread.start_new_thread(network_manager.wan_thread, (heartbeats,))
        
        # Wait up to 6 seconds for initial Wi-Fi connection to lock channel and finish AES handshake
        start_conn_wait = time.time()
        while time.time() - start_conn_wait < 6:
            if network_manager.is_connected():
                break
            if network_manager.startup_failed():
                break
            time.sleep_ms(200)

        if network_manager.startup_failed():
            print("Normal operation stopped because Wi-Fi could not be initialized.")
            return
            
        gc.collect()
        time.sleep_ms(200)
        
        # 6.2 Start MQTT thread
        _thread.start_new_thread(mqtt_client.mqtt_thread, (heartbeats,))
        time.sleep_ms(200)

        # 6.3 Start ESP-NOW Master receiver
        _thread.start_new_thread(espnow_master.espnow_receiver_thread, (heartbeats,))
        time.sleep_ms(200)

        # 6.4 Start Scheduler
        _thread.start_new_thread(scheduler.scheduler_thread, (heartbeats, espnow_master.send_espnow_msg))
    
    gc.collect()
    print("All Hub systems active! Free heap:", gc.mem_free())
    
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
