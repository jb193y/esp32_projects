import config
import wifiap


print(">>> boot.py STARTED <<<")

cfg = config.load_config()
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

else:
    print("AP mode selected")
    wifiap.start_ap_mode()

    print("Starting setup server")
    import server
    server.start_server()

print(">>> boot.py COMPLETED <<<")
