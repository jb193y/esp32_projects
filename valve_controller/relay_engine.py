# relay_engine.py (Valve Controller)
import ujson
import network
import espnow
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

def process_and_relay(packet):
    """
    Evaluates packet routing.
    Returns: True if packet target is us, False if packet was relayed or ignored.
    """
    global _e
    if _e is None:
        print("❌ Relay engine not initialized with ESPNow")
        return False
        
    msg_type = packet.get("msg_type")
    target_mac = packet.get("target_mac")
    routing_path = packet.get("routing_path", [])
    current_hop_index = packet.get("current_hop_index", 0)
    payload = packet.get("payload", {})
    
    sta = network.WLAN(network.STA_IF)
    local_mac = bytes_to_mac(sta.config('mac'))
    
    if not routing_path:
        if target_mac and target_mac.upper() == local_mac.upper():
            return True
        return False
        
    if current_hop_index >= len(routing_path):
        print("⚠️ current_hop_index out of routing path bounds")
        return False
        
    current_hop_mac = routing_path[current_hop_index]
    
    if current_hop_mac.upper() != local_mac.upper():
        return False
        
    if target_mac.upper() == local_mac.upper():
        print("🎯 Packet reached final target destination.")
        return True
        
    next_hop_index = current_hop_index + 1
    if next_hop_index >= len(routing_path):
        print("⚠️ Cannot relay: current hop matches us, but no next hop in path and we are not the target")
        return False
        
    next_hop_mac = routing_path[next_hop_index]
    next_hop_bytes = mac_to_bytes(next_hop_mac)
    
    packet["current_hop_index"] = next_hop_index
    
    print(f"🔄 Relaying packet to next hop: {next_hop_mac}")
    try:
        add_peer_safe(_e, next_hop_bytes)
        payload_str = ujson.dumps(packet)
        _e.send(next_hop_bytes, payload_str.encode('utf-8'))
        print("✅ Packet relayed successfully.")
    except Exception as e:
        print(f"❌ Failed to relay packet to next hop: {e}")
        
    return False
