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
    sta = network.WLAN(network.STA_IF)
    sta.active(False)      # Important

    ap = network.WLAN(network.AP_IF)
    ap.active(True)

    ap.config(
        essid="ESP32_Setup",
        password="12345678",
        authmode=3
    )

    print("📡 AP started:", ap.ifconfig())
    return ap
