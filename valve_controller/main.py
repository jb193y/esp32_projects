# main.py (Valve Controller)
import _thread
import time
import machine
import os
import gc
import config
import led_status
import ble_manager
import relay_engine
import espnow
import ujson

# State
solenoid_open_pin = None
solenoid_close_pin = None
valve_state = "CLOSED" # "OPEN", "CLOSED"
paired = False
hub_mac_bytes = None
parent_mac_bytes = None

def mac_to_bytes(mac_str):
    return bytes(int(x, 16) for x in mac_str.split(':'))

def bytes_to_mac(mac_bytes):
    return ':'.join('%02x' % b for b in mac_bytes)

def add_peer_safe(e, peer_bytes):
    try:
        e.add_peer(peer_bytes)
    except OSError as err:
        if err.args[0] == 17: # EEXIST
            return
        print("⚠️ ESP-NOW Peer limit reached, cleaning up...")
        try:
            peers = e.peers() if callable(getattr(e, 'peers', None)) else e.peers
        except:
            peers = []
        if peers:
            try:
                e.del_peer(peers[0])
                e.add_peer(peer_bytes)
            except Exception as ex:
                print("❌ Failed to resolve peer slots:", ex)
                raise err

def pulse_solenoid(open_pulse=True):
    global solenoid_open_pin, solenoid_close_pin, valve_state
    if solenoid_open_pin is None or solenoid_close_pin is None:
        return
        
    print(f"🚰 Solenoid Action: {'OPENING' if open_pulse else 'CLOSING'} pulse starting...")
    
    solenoid_open_pin.value(0)
    solenoid_close_pin.value(0)
    time.sleep_ms(10)
    
    if open_pulse:
        solenoid_open_pin.value(1)
        time.sleep_ms(100) # 100ms pulse
        solenoid_open_pin.value(0)
        valve_state = "OPEN"
        led_status.set_status("VALVE_OPEN")
    else:
        solenoid_close_pin.value(1)
        time.sleep_ms(100) # 100ms pulse
        solenoid_close_pin.value(0)
        valve_state = "CLOSED"
        led_status.set_status("VALVE_CLOSED")
        
    print(f"🚰 Solenoid Action complete. State: {valve_state}")

def send_ack_or_tele_to_hub(e, msg_type, payload):
    cfg = config.load_config()
    hub_mac_str = cfg.get("hub", {}).get("mac", "00:00:00:00:00:00")
    parent_mac_str = cfg.get("parent", {}).get("mac", "00:00:00:00:00:00")
    
    routing_path = []
    if parent_mac_str != "00:00:00:00:00:00" and parent_mac_str != hub_mac_str:
        routing_path = [parent_mac_str, hub_mac_str]
    else:
        routing_path = [hub_mac_str]
        
    packet = {
        "msg_type": msg_type,
        "target_mac": hub_mac_str,
        "routing_path": routing_path,
        "current_hop_index": 0,
        "payload": payload
    }
    
    next_hop = routing_path[0]
    next_hop_bytes = mac_to_bytes(next_hop)
    
    try:
        add_peer_safe(e, next_hop_bytes)
        payload_str = ujson.dumps(packet)
        e.send(next_hop_bytes, payload_str.encode('utf-8'))
        print(f"🛫 ACK/TELE sent to next hop {next_hop} for Hub")
        return True
    except Exception as err:
        print("❌ Failed to transmit packet back to Hub:", err)
        return False

def send_pairing_request(e):
    cfg = config.load_config()
    client_cfg = cfg.get("client", {})
    payload = {
        "node_type": "VALVE",
        "custom_name": client_cfg.get("custom_name", "Valve Node")
    }
    print("🤝 Sending PAIR_REQ from Valve...")
    send_ack_or_tele_to_hub(e, "PAIR_REQ", payload)

def handle_local_commands(e, cmd, args):
    global valve_state
    print(f"⚙️ Handling local command: {cmd}")
    
    if cmd == "VALVE_OPEN":
        pulse_solenoid(open_pulse=True)
        send_ack_or_tele_to_hub(e, "ACK", {"valve_state": valve_state, "node_status": "active"})
        
    elif cmd == "VALVE_CLOSE":
        pulse_solenoid(open_pulse=False)
        send_ack_or_tele_to_hub(e, "ACK", {"valve_state": valve_state, "node_status": "active"})
        
    elif cmd == "GET_STATUS":
        send_ack_or_tele_to_hub(e, "ACK", {"valve_state": valve_state, "node_status": "active"})

def main():
    global solenoid_open_pin, solenoid_close_pin, paired, valve_state
    print("🚀 Valve Controller Starting...")
    
    # 1. Start Status LED
    _thread.start_new_thread(led_status.led_thread, ())
    
    # 2. Load configurations
    cfg_exists = "config.json" in os.listdir()
    cfg = None
    if cfg_exists:
        try:
            cfg = config.load_config()
        except Exception:
            cfg = None
            
    if cfg is None:
        print("⚠️ Configuration missing or corrupt! Falling back to BLE Provisioning Mode.")
        led_status.set_status("BLE_PROVISIONING")
        ble_manager.start_provisioning()
        return
        
    client_cfg = cfg.get("client", {})
    mode = client_cfg.get("mode", "ble_setup")
    
    if mode == "ble_setup":
        print("📡 BLE Setup mode configured. Initializing provisioning...")
        led_status.set_status("BLE_PROVISIONING")
        ble_manager.start_provisioning()
        return

    # 3. Start Normal Operations
    print("📶 Loading Valve Outputs...")
    led_status.set_status("VALVE_CLOSED")
    valve_state = "CLOSED"
    
    pins = cfg.get("valve", {}).get("pins", {})
    open_pin_num = pins.get("solenoid_open", 18)
    close_pin_num = pins.get("solenoid_close", 19)
    
    solenoid_open_pin = machine.Pin(open_pin_num, machine.Pin.OUT)
    solenoid_close_pin = machine.Pin(close_pin_num, machine.Pin.OUT)
    
    solenoid_open_pin.value(0)
    solenoid_close_pin.value(0)
    
    sta = machine.WLAN(machine.STA_IF)
    sta.active(True)
    
    e = espnow.ESPNow()
    e.active(True)
    
    relay_engine.init_relay_engine(e)
    
    send_pairing_request(e)
    last_tele_time = time.time()
    
    print("✅ Valve Node ready and running loop.")
    
    while True:
        try:
            gc.collect()
            
            host, msg = e.recv(200)
            if host is not None and msg is not None:
                sender_mac = bytes_to_mac(host)
                payload_str = msg.decode('utf-8')
                print(f"📥 Received packet from {sender_mac}: {payload_str}")
                
                packet = ujson.loads(payload_str)
                
                msg_type = packet.get("msg_type")
                if msg_type == "ACK" and packet.get("target_mac") == bytes_to_mac(sta.config('mac')):
                    if packet.get("payload", {}).get("status") == "paired":
                        paired = True
                        print("✅ Valve paired successfully with Hub.")
                
                is_for_us = relay_engine.process_and_relay(packet)
                if is_for_us:
                    payload = packet.get("payload", {})
                    cmd = payload.get("cmd")
                    handle_local_commands(e, cmd, payload)
            else:
                if not paired:
                    send_pairing_request(e)
                    time.sleep(5)
                else:
                    now = time.time()
                    if now - last_tele_time >= 10:
                        last_tele_time = now
                        send_ack_or_tele_to_hub(e, "TELE", {
                            "node_id": client_cfg.get("id"),
                            "status": "valve_idle",
                            "valve_state": valve_state,
                            "rssi": -50
                        })
                    time.sleep_ms(50)
                    
        except Exception as err:
            print("⚠️ Valve Loop Error:", err)
            time.sleep(1)

if __name__ == "__main__":
    main()
