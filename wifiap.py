import network
import time
import config

def connect_wifi(networks):
    """
    Try to connect to multiple Wi-Fi networks.
    Returns True if connected successfully, otherwise False.
    """
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)

    for net in networks:
        ssid = net.get("ssid")
        pwd = net.get("password")
        print(f"Trying Wi-Fi: {ssid}")
        wlan.connect(ssid, pwd)

        for _ in range(15):  # wait up to ~15 seconds
            if wlan.isconnected():
                print("Connected:", wlan.ifconfig())
                return True
            time.sleep(1)

        print(f"Failed to connect to {ssid}")

    wlan.active(False)
    return False


def start_ap_mode():
    """
    Start ESP32 in Access Point mode.
    Broadcasts SSID as ESP32_<device_id>.
    """
    cfg = config.load_config()
    ap = network.WLAN(network.AP_IF)
    ap.active(True)

    ssid = f"ESP32_{cfg.get('device_id', 'Device')}"
    password = "12345678"  # Minimum 8 chars required

    ap.config(essid=ssid, password=password, authmode=3)
    print(f"Access Point started: {ssid}")
    print("AP IP config:", ap.ifconfig())

    return ap
