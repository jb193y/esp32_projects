# network_manager.py (Hub)
import network
import time
import config
import led_status

_is_wan_connected = False

def is_connected():
    try:
        wlan = network.WLAN(network.STA_IF)
        return wlan.isconnected()
    except Exception:
        return _is_wan_connected

def connect_wifi(networks, wlan=None, timeout=15):
    if wlan is None:
        wlan = network.WLAN(network.STA_IF)
    if not wlan.active():
        wlan.active(True)
    
    if wlan.isconnected():
        return True

    for net in networks:
        ssid = net.get("ssid")
        password = net.get("password", "")
        if not ssid:
            continue
            
        print(f"Connecting to SSID: {ssid}...")
        try:
            wlan.connect(ssid, password)
        except Exception as conn_err:
            print("wlan.connect error:", conn_err)
        
        # Wait for connection
        start_time = time.time()
        while not wlan.isconnected():
            if time.time() - start_time > timeout:
                print(f"Connection to {ssid} timed out.")
                break
            time.sleep(0.5)
            
        if wlan.isconnected():
            print("Connected! IP details:", wlan.ifconfig())
            return True
            
    return False

def wan_thread(heartbeats=None):
    global _is_wan_connected
    print("Network Manager WAN Thread Started")
    
    cfg = config.load_config()
    networks = cfg.get("wifi", {}).get("networks", [])
    wlan = network.WLAN(network.STA_IF)
    if not wlan.active():
        try:
            wlan.active(True)
        except Exception as act_ex:
            print("WLAN activation notice:", act_ex)
    
    while True:
        if heartbeats is not None:
            heartbeats["network"] = time.time()
            
        if not wlan.isconnected():
            _is_wan_connected = False
            led_status.set_status("WIFI_CONNECTING")
            print("WAN Disconnected! Reconnecting...")
            
            connected = connect_wifi(networks, wlan)
            if connected:
                _is_wan_connected = True
                led_status.set_status("WIFI_CONNECTED")
                try:
                    import ntptime
                    ntptime.host = "pool.ntp.org"
                    ntptime.settime()
                    print(" NTP synchronization successful. Local time:", time.localtime())
                except Exception as ntp_err:
                    print(" NTP sync failed:", ntp_err)
            else:
                print("WiFi Reconnection failed. Retrying in 10s...")
                time.sleep(10)
                continue
        else:
            _is_wan_connected = True
            
        time.sleep(5)
