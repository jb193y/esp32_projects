# boot.py
import time

print(">>> boot.py STARTED <<<")

# Give mpremote time to interrupt
time.sleep(3)

import config
import wifiap
import led_status

cfg = config.load_config()

# 1) Restore time ASAP
time_restored = config.restore_time(cfg)

# 2) Start Wi-Fi in selected mode
mode = cfg.get("client", {}).get("mode", "ap")

if mode == "sta":
    print("STA mode selected")
    led_status.set_status("WIFI_CONNECTING")
    networks = cfg.get("wifi", {}).get("networks", [])
    connected = wifiap.connect_wifi(networks)

    if not connected:
        print("Wi-Fi failed → entering AP setup mode")
        led_status.set_status("AP_MODE")
        wifiap.start_ap_mode()

        print("Starting setup server")
        import server
        server.start_server()
    else:
        # Successfully connected to Wi-Fi
        led_status.set_status("WIFI_CONNECTED")

    # 3) Sync time via NTP if enabled and not restored
    ntp_cfg = cfg.get("time", {}).get("ntp", {})
    if (not time_restored) and ntp_cfg.get("sync_on_boot", True):
        config.sync_time_ntp(cfg)

else:
    print("AP mode selected")
    led_status.set_status("AP_MODE")
    wifiap.start_ap_mode()

    print("Starting setup server")
    import server
    server.start_server()

print(">>> boot.py COMPLETED <<<")
