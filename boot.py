# boot.py
import time

print(">>> boot.py STARTED <<<")

# Give mpremote time to interrupt
time.sleep(3)

import config
import wifiap

cfg = config.load_config()

# Restore time ASAP
time_restored = config.restore_time(cfg)

# Mode
mode = cfg.get("device", {}).get("mode", "ap")

if mode == "sta":
    print("STA mode selected")
    networks = cfg.get("wifi", {}).get("networks", [])
    connected = wifiap.connect_wifi(networks)

    if not connected:
        print("Wi-Fi failed → entering AP setup mode")
        wifiap.start_ap_mode()
        print("Starting setup server")
        import server
        server.start_server()

    # Sync time if not restored
    if not time_restored:
        config.sync_time_ntp(cfg)

else:
    print("AP mode selected")
    wifiap.start_ap_mode()
    print("Starting setup server")
    import server
    server.start_server()

print(">>> boot.py COMPLETED <<<")
