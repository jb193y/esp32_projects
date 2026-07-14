# network_manager.py (Hub)
import network
import time
import config
import led_status

_is_wan_connected = False

def is_connected():
    return _is_wan_connected

def connect_wifi(networks, timeout=15):
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    
    if wlan.isconnected():
        return True

    print("📶 Scanning for Wi-Fi networks...")
    try:
        visible_aps = wlan.scan()
        visible_ssids = {ap[0].decode('utf-8', 'ignore') for ap in visible_aps}
    except Exception as e:
        print("⚠️ Scan failed:", e)
        visible_ssids = set()

    for net in networks:
        ssid = net.get("ssid")
        password = net.get("password", "")
        
        if ssid and ssid not in visible_ssids:
            print(f"ℹ️ Configured SSID '{ssid}' not visible. Skipping...")
            continue
            
        print(f"🔌 Connecting to SSID: {ssid}...")
        wlan.connect(ssid, password)
        
        # Wait for connection
        start_time = time.time()
        while not wlan.isconnected():
            if time.time() - start_time > timeout:
                print(f"❌ Connection to {ssid} timed out.")
                break
            time.sleep(0.5)
            
        if wlan.isconnected():
            print("✅ Connected! IP details:", wlan.ifconfig())
            return True
            
    return False

def wan_thread(heartbeats=None):
    global _is_wan_connected
    print("🚀 Network Manager WAN Thread Started")
    
    cfg = config.load_config()
    networks = cfg.get("wifi", {}).get("networks", [])
    
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    
    while True:
        if heartbeats is not None:
            heartbeats["network"] = time.time()
            
        if not wlan.isconnected():
            _is_wan_connected = False
            led_status.set_status("WIFI_CONNECTING")
            print("⚠️ WAN Disconnected! Reconnecting...")
            
            connected = connect_wifi(networks)
            if connected:
                _is_wan_connected = True
                led_status.set_status("WIFI_CONNECTED")
            else:
                print("❌ WiFi Reconnection failed. Retrying in 10s...")
                time.sleep(10)
                continue
        else:
            _is_wan_connected = True
            
        time.sleep(5)
