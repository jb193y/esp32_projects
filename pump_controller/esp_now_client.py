# esp_now_client.py (Pump Controller)
import network
import espnow
import ujson
import time
import config

_e = None
_paired = False
_hub_mac_bytes = None

def mac_to_bytes(mac_str):
    return bytes(int(x, 16) for x in mac_str.split(':'))

def bytes_to_mac(mac_bytes):
    return ':'.join('%02x' % b for b in mac_bytes)

def is_paired():
    return _paired

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

def send_to_hub(msg_type, payload):
    global _e, _hub_mac_bytes
    if _e is None or _hub_mac_bytes is None:
        return False
        
    cfg = config.load_config()
    hub_mac_str = cfg.get("hub", {}).get("mac", "00:00:00:00:00:00")
    
    packet = {
        "msg_type": msg_type,
        "target_mac": hub_mac_str,
        "routing_path": [hub_mac_str],
        "current_hop_index": 0,
        "payload": payload
    }
    
    try:
        add_peer_safe(_e, _hub_mac_bytes)
        payload_str = ujson.dumps(packet)
        _e.send(_hub_mac_bytes, payload_str.encode('utf-8'))
        return True
    except Exception as e:
        print("❌ ESP-NOW transmission to Hub failed:", e)
        return False

def send_pairing_request():
    global _hub_mac_bytes
    if _hub_mac_bytes is None:
        return
        
    cfg = config.load_config()
    client_cfg = cfg.get("client", {})
    
    payload = {
        "node_type": "PUMP",
        "custom_name": client_cfg.get("custom_name", "Pump Node")
    }
    print("🤝 Sending PAIR_REQ to Hub...")
    send_to_hub("PAIR_REQ", payload)

def init_espnow_client(on_cmd_received_fn):
    global _e, _hub_mac_bytes, _paired
    print("🚀 Initializing ESP-NOW Client...")
    
    sta = network.WLAN(network.STA_IF)
    sta.active(True)
    
    cfg = config.load_config()
    hub_mac_str = cfg.get("hub", {}).get("mac", "00:00:00:00:00:00")
    if hub_mac_str == "00:00:00:00:00:00":
        print("⚠️ Hub MAC is unconfigured! Peer pairing skipped.")
        return False
        
    _hub_mac_bytes = mac_to_bytes(hub_mac_str)
    
    _e = espnow.ESPNow()
    _e.active(True)
    
    send_pairing_request()
    return True

def client_listen_loop(heartbeats=None, on_cmd_received_fn=None):
    global _e, _paired, _hub_mac_bytes
    if _e is None:
        return
        
    while True:
        if heartbeats is not None:
            heartbeats["esp_now"] = time.time()
            
        try:
            host, msg = _e.recv(200)
            if host is None or msg is None:
                if not _paired:
                    send_pairing_request()
                    time.sleep(5)
                else:
                    time.sleep_ms(50)
                continue
                
            sender_mac_str = bytes_to_mac(host)
            payload_str = msg.decode('utf-8')
            print(f"📥 ESP-NOW Client Received: {payload_str}")
            
            packet = ujson.loads(payload_str)
            msg_type = packet.get("msg_type")
            payload = packet.get("payload", {})
            
            if msg_type == "ACK" and payload.get("status") == "paired":
                _paired = True
                print("✅ Paired successfully with Hub!")
                
            elif msg_type == "CMD" and on_cmd_received_fn is not None:
                cmd = payload.get("cmd")
                on_cmd_received_fn(cmd, payload)
                
        except Exception as e:
            time.sleep_ms(100)
