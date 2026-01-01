import config
import wifiap


print(">>> boot.py STARTED <<<")

cfg = config.load_config()
# 1️⃣ Restore time immediately
time_restored = config.restore_time(cfg)

# 3️⃣ Start Wi-Fi in selected mode
mode = cfg.get("mode", "ap")

if mode == "sta":
    print("STA mode selected")
    connected = wifiap.connect_wifi(cfg.get("wifi_networks", []))

    if not connected:
        print("Wi-Fi failed → entering AP setup mode")
        wifiap.start_ap_mode()

        print("Starting setup server")
        import server
        server.start_server()
        
    # 2️⃣ Sync time via NTP if enabled and not restored
    if not time_restored:
        config.sync_time_ntp(cfg)

else:
    print("AP mode selected")
    wifiap.start_ap_mode()

    print("Starting setup server")
    import server
    server.start_server()

print(">>> boot.py COMPLETED <<<")
