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

def _format_wifi_status(st):
    mapping = {
        1000: "STAT_IDLE",
        1001: "STAT_CONNECTING (Still negotiating handshake)",
        1010: "STAT_GOT_IP",
        201: "STAT_NO_AP_FOUND (SSID not found - verify 2.4GHz hotspot & range)",
        202: "STAT_WRONG_PASSWORD (Authentication failed - check password)",
        203: "STAT_CONNECT_FAIL (Connection failed / rejected by AP)",
        204: "STAT_HANDSHAKE_TIMEOUT",
        200: "STAT_BEACON_TIMEOUT",
    }
    return mapping.get(st, f"CODE_{st}")

def _scan_and_check(wlan, target_ssid):
    try:
        results = wlan.scan()
        ssids = []
        found_target = False
        for ap in results:
            try:
                name = ap[0].decode('utf-8')
                if name:
                    entry = f"'{name}' (ch {ap[2]}, {ap[3]}dBm)"
                    ssids.append(entry)
                    if name == target_ssid:
                        found_target = entry
            except Exception:
                pass
        
        if found_target:
            print(f" [Wi-Fi Scan] '{target_ssid}' IS detected on 2.4GHz ({found_target}). Issue is likely password/auth!")
        else:
            print(f" [Wi-Fi Scan] '{target_ssid}' NOT found in 2.4GHz scan! (Visible: {ssids[:5]})")
            print(f" -> If using a phone hotspot, enable 'Maximize Compatibility' (iPhone) or '2.4 GHz Band' (Android).")
    except Exception as ex:
        print(" [Wi-Fi Scan] Scan check notice:", ex)

def connect_wifi(networks, wlan=None, timeout=15):
    if wlan is None:
        wlan = network.WLAN(network.STA_IF)
    if not wlan.active():
        try:
            wlan.active(True)
        except Exception:
            pass
    
    if wlan.isconnected():
        return True

    for net in networks:
        ssid = net.get("ssid")
        password = net.get("password", "")
        if not ssid:
            continue
            
        print(f"Connecting to SSID: {ssid}...")
        
        # Abort any prior in-flight connection so ESP-IDF is in clean IDLE state
        try:
            wlan.disconnect()
        except Exception:
            pass
        time.sleep_ms(100)
        
        try:
            wlan.connect(ssid, password)
        except Exception as conn_err:
            print("wlan.connect error:", conn_err)
            # Cycle interface on internal state error
            try:
                wlan.disconnect()
                time.sleep_ms(50)
                wlan.active(False)
                time.sleep_ms(50)
                wlan.active(True)
            except Exception:
                pass
            continue
        
        # Wait for connection
        start_time = time.ticks_ms()
        while not wlan.isconnected():
            if time.ticks_diff(time.ticks_ms(), start_time) > timeout * 1000:
                st = None
                try:
                    st = wlan.status()
                except Exception:
                    pass
                print(f"Connection to {ssid} timed out (Status: {_format_wifi_status(st)}).")
                
                # Check 2.4GHz visibility to pinpoint hotspot issues
                _scan_and_check(wlan, ssid)
                
                # Disconnect cleanly to release ESP-IDF connecting state
                try:
                    wlan.disconnect()
                except Exception:
                    pass
                time.sleep_ms(100)
                break
                
            time.sleep(0.5)
            
        if wlan.isconnected():
            print("Connected! IP details:", wlan.ifconfig())
            return True
            
    return False

def wan_thread(heartbeats=None):
    global _is_wan_connected, _wlan
    print("Network Manager WAN Thread Started")
    
    cfg = config.load_config()
    networks = cfg.get("wifi", {}).get("networks", [])
    wlan = network.WLAN(network.STA_IF)
    _wlan = wlan
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
        else:
            _is_wan_connected = True
            
        time.sleep(5)
