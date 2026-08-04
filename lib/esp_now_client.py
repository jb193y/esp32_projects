# esp_now_client.py (Shared ESP-NOW Client Library)
import network
import espnow
import ujson
import time
import config
import relay_engine

_e = None
_paired = False

def mac_to_bytes(mac_str):
    return bytes(int(x, 16) for x in mac_str.split(':'))

def bytes_to_mac(mac_bytes):
    return ':'.join('%02x' % b for b in mac_bytes)

def is_paired():
    return _paired

def set_paired(val):
    global _paired
    _paired = val

def add_peer_safe(e, peer_bytes):
    try:
        peers_list = e.peers() if callable(getattr(e, 'peers', None)) else e.peers
        peer_macs = [bytes(p[0]) for p in peers_list]
    except Exception:
        peer_macs = []

    peer_bytes = bytes(peer_bytes)

    if peer_bytes in peer_macs:
        return

    try:
        e.add_peer(peer_bytes)
    except OSError as err:
        print("⚠️ ESP-NOW Peer limit reached, cleaning up...")
        try:
            peers_list = e.peers() if callable(getattr(e, 'peers', None)) else e.peers
        except:
            peers_list = []
        if peers_list:
            try:
                e.del_peer(peers_list[0][0])
                e.add_peer(peer_bytes)
            except Exception as ex:
                print("❌ Failed to resolve peer slots:", ex)
                raise err

def send_ack_or_tele_to_hub(msg_type, payload, target_mac=None):
    global _e
    if _e is None:
        return False
        
    cfg = config.load_config()
    hub_mac_str = cfg.get("hub", {}).get("mac", "00:00:00:00:00:00")
    dest_mac = target_mac if target_mac is not None else hub_mac_str
    
    parent_mac_str = cfg.get("parent", {}).get("mac", "00:00:00:00:00:00")
    
    routing_path = []
    if parent_mac_str != "00:00:00:00:00:00" and parent_mac_str != dest_mac:
        routing_path = [parent_mac_str, dest_mac]
    else:
        routing_path = [dest_mac]
        
    packet = {
        "msg_type": msg_type,
        "target_mac": dest_mac,
        "routing_path": routing_path,
        "current_hop_index": 0,
        "payload": payload
    }
    
    next_hop = routing_path[0]
    next_hop_bytes = mac_to_bytes(next_hop)
    
    try:
        add_peer_safe(_e, next_hop_bytes)
        payload_str = ujson.dumps(packet)
        _e.send(next_hop_bytes, payload_str.encode('utf-8'))
        print(f"🛫 Packet sent to next hop {next_hop} for destination {dest_mac}")
        return True
    except Exception as err:
        print(f"❌ Failed to transmit packet back to destination {dest_mac}:", err)
        return False

# Backward compatibility alias for pump controller
def send_to_hub(msg_type, payload):
    return send_ack_or_tele_to_hub(msg_type, payload)

def send_pairing_request():
    global _e
    if _e is None:
        return

    cfg = config.load_config()
    client_cfg = cfg.get("client", {})
    node_type = client_cfg.get("type", "client").upper()
    payload = {
        "node_type": node_type,
        "node_id": client_cfg.get("id", ""),
        "custom_name": client_cfg.get("custom_name", "Client Node")
    }
    print(f"🤝 Sending PAIR_REQ from {node_type}...")
    send_ack_or_tele_to_hub("PAIR_REQ", payload)


def init_espnow_client(on_cmd_received_fn=None):
    global _e
    cfg = config.load_config()
    client_name = cfg.get("client", {}).get("custom_name", "Client Node")
    print(f"🚀 Initializing {client_name} ESP-NOW Client...")
    
    sta = network.WLAN(network.STA_IF)
    sta.active(True)
    
    _e = espnow.ESPNow()
    _e.active(True)
    
    relay_engine.init_relay_engine(_e)
    
    send_pairing_request()
    return _e

def client_listen_loop(heartbeats=None, on_cmd_received_fn=None):
    global _e, _paired
    if _e is None:
        return
        
    sta = network.WLAN(network.STA_IF)
    local_mac = bytes_to_mac(sta.config('mac'))
    
    while True:
        if heartbeats is not None:
            heartbeats["esp_now"] = time.time()
            
        try:
            host, msg = _e.recv(200)
            if host is not None and msg is not None:
                sender_mac = bytes_to_mac(host)
                payload_str = msg.decode('utf-8')
                print(f"📥 Received packet from {sender_mac}: {payload_str}")
                
                packet = ujson.loads(payload_str)
                msg_type = packet.get("msg_type")
                target_mac = packet.get("target_mac")
                
                if msg_type == "ACK" and (target_mac == local_mac or target_mac is None):
                    if packet.get("payload", {}).get("status") == "paired":
                        _paired = True
                        print("✅ Client paired successfully with Hub.")
                
                # Relaying and target validation
                is_for_us = False
                if target_mac is None:
                    # Legacy fallback
                    is_for_us = (msg_type == "CMD" or msg_type == "ACK")
                else:
                    is_for_us = relay_engine.process_and_relay(packet)
                    
                if is_for_us and on_cmd_received_fn is not None:
                    payload = packet.get("payload", {})
                    cmd = payload.get("cmd")
                    
                    # Inject sender_mac (original sender or immediate hop) to allow communicating back
                    payload["sender_mac"] = packet.get("sender_mac", sender_mac)
                    payload["routing_path"] = packet.get("routing_path", [])
                    
                    on_cmd_received_fn(cmd, payload)
            else:
                if not _paired:
                    send_pairing_request()
                    time.sleep(5)
                else:
                    time.sleep_ms(50)
                    
        except Exception as err:
            print("⚠️ Client loop error:", err)
            time.sleep(1)
