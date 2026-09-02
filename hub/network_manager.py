# network_manager.py (Hub)
import network
import time
import config
import led_status

_is_wan_connected = False
_wlan = None
_wan_startup_failed = False

def is_connected():
    return _is_wan_connected

def startup_failed():
    return _wan_startup_failed

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
    global _is_wan_connected, _wlan, _wan_startup_failed
    print("Network Manager WAN Thread Started")
    
    cfg = config.load_config()
    networks = cfg.get("wifi", {}).get("networks", [])
    try:
        wlan = network.WLAN(network.STA_IF)
    except Exception as wlan_err:
        _wan_startup_failed = True
        print("Wi-Fi initialization failed:", wlan_err)
        if "Memory" in str(wlan_err) or "0x3001" in str(wlan_err):
            print(" Soft reboot DMA leak detected. Performing clean hardware reset...")
            time.sleep_ms(300)
            import machine
            machine.reset()
        print("Wi-Fi is unavailable; use firmware built for this board's PSRAM configuration.")
        return
    _wlan = wlan
    if not wlan.active():
        try:
            wlan.active(True)
        except Exception as act_ex:
            print("WLAN activation notice:", act_ex)
            if "Memory" in str(act_ex) or "0x3001" in str(act_ex):
                print(" Soft reboot Wi-Fi DMA state corrupted. Performing clean hardware reset...")
                time.sleep_ms(300)
                import machine
                machine.reset()
    
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
