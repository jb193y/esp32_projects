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

heartbeats = {
    "network": time.time(),
    "mqtt": time.time(),
    "esp_now": time.time(),
    "scheduler": time.time()
}

def watchdog_thread():
    print("Watchdog Thread Active")
    last_checked_system_time = time.time()
    while True:
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

def main():
    print("Hub Master Controller Loading...")
    
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
    
    if mode == "ble_setup":
        print("BLE Setup mode configured. Initializing provisioning...")
        led_status.set_status("BLE_PROVISIONING")
        ble_manager.start_provisioning()
        return
        
    # 3. Start Normal Operations (STA Mode)
    print("Booting into STA normal operations...")
    led_status.set_status("WIFI_CONNECTING")
    
    # Register command callbacks
    mqtt_client.register_cmd_dispatcher(esp_now_master.dispatch_command_from_mqtt)
    
    # Launch worker threads
    _thread.start_new_thread(network_manager.wan_thread, (heartbeats,))
    _thread.start_new_thread(mqtt_client.mqtt_thread, (heartbeats,))
    _thread.start_new_thread(esp_now_master.espnow_receiver_thread, (heartbeats,))
    _thread.start_new_thread(scheduler.scheduler_thread, (heartbeats, esp_now_master.send_espnow_msg))
    _thread.start_new_thread(watchdog_thread, ())
    
    print("All Hub systems active!")
    
    while True:
        gc.collect()
        time.sleep(10)

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("Main loop error:", e)
