# relay_engine.py (Shared Relay Engine Library)
import ujson
import network
try:
    import espnow
except ImportError:
    espnow = None
import config

_e = None

def mac_to_bytes(mac_str):
    return bytes(int(x, 16) for x in mac_str.split(':'))

def bytes_to_mac(mac_bytes):
    return ':'.join('%02x' % b for b in mac_bytes)

def init_relay_engine(espnow_instance):
    global _e
    _e = espnow_instance

def add_peer_safe(e, peer_bytes):
    try:
        e.add_peer(peer_bytes)
    except Exception as err:
        err_str = str(err)
        if "EXIST" in err_str or "17" in err_str or "116" in err_str or "12301" in err_str:
            return
        try:
            peers = e.get_peers() if hasattr(e, 'get_peers') else []
            if peers:
                e.del_peer(peers[0][0])
                e.add_peer(peer_bytes)
        except Exception:
            pass

def parse_packet(payload_str):
    p = ujson.loads(payload_str)
    msg_type = p.get("msg_type") or p.get("t")
    target_mac = p.get("target_mac") or p.get("dst", "")
    routing_path = p.get("routing_path") or p.get("path", [])
    current_hop_index = p.get("current_hop_index") if "current_hop_index" in p else p.get("hop", 0)
    payload = p.get("payload") if "payload" in p else p.get("pld", {})
    return {
        "msg_type": msg_type,
        "target_mac": target_mac,
        "routing_path": routing_path,
        "current_hop_index": current_hop_index,
        "payload": payload
    }

def process_and_relay(packet):
    """
    Evaluates packet routing.
    Returns: True if packet target is us, False if packet was relayed or ignored.
    """
    global _e
    if _e is None:
        print(" Relay engine not initialized with ESPNow")
        return False
        
    msg_type = packet.get("msg_type") or packet.get("t")
    target_mac = packet.get("target_mac") or packet.get("dst")
    routing_path = packet.get("routing_path") or packet.get("path", [])
    current_hop_index = packet.get("current_hop_index") if "current_hop_index" in packet else packet.get("hop", 0)
    payload = packet.get("payload", {})
    
    sta = network.WLAN(network.STA_IF)
    local_mac = bytes_to_mac(sta.config('mac'))
    
    # If the packet is targeted at us, return True immediately
    if target_mac and target_mac.upper() == local_mac.upper():
        print(" Packet reached final target destination.")
        return True
        
    if not routing_path:
        return False
        
    if current_hop_index >= len(routing_path):
        print(" current_hop_index out of routing path bounds")
        return False
        
    current_hop_mac = routing_path[current_hop_index]
    
    if current_hop_mac.upper() != local_mac.upper():
        return False
        
    if target_mac.upper() == local_mac.upper():
        print(" Packet reached final target destination.")
        return True
        
    next_hop_index = current_hop_index + 1
    if next_hop_index >= len(routing_path):
        print(" Cannot relay: current hop matches us, but no next hop in path and we are not the target")
        return False
        
    next_hop_mac = routing_path[next_hop_index]
    next_hop_bytes = mac_to_bytes(next_hop_mac)
    
    packet["current_hop_index"] = next_hop_index
    
    print(f" Relaying packet to next hop: {next_hop_mac}")
    try:
        add_peer_safe(_e, next_hop_bytes)
        payload_str = ujson.dumps(packet)
        _e.send(next_hop_bytes, payload_str.encode('utf-8'))
        print(" Packet relayed successfully.")
    except Exception as e:
        print(f" Failed to relay packet to next hop: {e}")
        
    return False
