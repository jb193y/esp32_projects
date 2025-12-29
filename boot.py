import config
import wifiap
import main

def main_boot():
    cfg = config.load_config()
    mode = cfg.get('mode', 'ap')

    if mode == 'sta':
        print("Starting in Station mode...")
        ssids = cfg.get('wifi_networks', [])
        connected = wifiap.connect_wifi(ssids)
        if not connected:
            print("Failed to connect, switching to Access Point mode.")
            wifiap.start_ap_mode()
            cfg['mode'] = 'ap'
            config.save_config(cfg)
            print("Launching setup server...")
            import server
            server.start_server()
        else:
            print("WiFi connected. Proceeding with normal operation.")
            main.run_main()
    else:
        print("Starting in Access Point mode...")
        wifiap.start_ap_mode()
        print("Launching setup server...")
        import server
        server.start_server()

main_boot()
   