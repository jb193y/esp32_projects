import network
import time

def connect_wifi(networks):
    # --- HARD RESET WIFI STATE ---
    ap = network.WLAN(network.AP_IF)
    sta = network.WLAN(network.STA_IF)

    ap.active(False)        # ❗ MUST disable AP
    time.sleep(0.5)

    sta.active(False)       # ❗ reset STA
    time.sleep(0.5)

    sta.active(True)
    time.sleep(0.5)

    sta.config(pm=network.WLAN.PM_NONE) # Disable power management for stability
    for net in networks:
        ssid = net.get("ssid")
        pwd = net.get("password")

        print(f"Trying Wi-Fi: {ssid}")

        try:
            sta.connect(ssid, pwd)
        except Exception as e:
            print("WiFi connect call failed:", e)
            continue

        for i in range(15):
            if sta.isconnected():
                print("✅ Connected:", sta.ifconfig())
                return True
            time.sleep(1)

        print(f"❌ Failed to connect to {ssid}")

        # Important: abort current attempt cleanly
        sta.disconnect()
        time.sleep(1)

    print("❌ All Wi-Fi networks failed")
    sta.active(False)
    return False

def start_ap_mode():
    import config
    cfg = config.load_config()
    server_cfg = cfg.get("server", {})
    ssid = server_cfg.get("ap_ssid", "ESP32_Pump_Setup")
    password = server_cfg.get("ap_password", "12345678")

    sta = network.WLAN(network.STA_IF)
    sta.active(False)      # Important
    time.sleep(0.1)

    ap = network.WLAN(network.AP_IF)
    ap.active(False)       # Deactivate first to reset DHCP daemon state
    time.sleep(0.2)

    # Configure SSID & Security BEFORE activating the AP interface
    if len(password) >= 8:
        ap.config(essid=ssid, password=password, authmode=3)
    else:
        ap.config(essid=ssid, authmode=0)

    # Set explicit Gateway IP configuration to ensure DHCP leases match
    ap.ifconfig(('192.168.4.1', '255.255.255.0', '192.168.4.1', '8.8.8.8'))
    
    # Finally activate
    ap.active(True)
    time.sleep(0.5)

    print("📡 AP started:", ap.ifconfig())
    return ap
