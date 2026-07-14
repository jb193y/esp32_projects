# esp_now_master.py (Hub)
import network
import espnow
import ujson
import time
import ubinascii
import os
import config
import mqtt_client
import scheduler

_e = None
NODES_FILE = "nodes.json"

def mac_to_bytes(mac_str):
    return bytes(int(x, 16) for x in mac_str.split(':'))

def bytes_to_mac(mac_bytes):
    return ':'.join('%02x' % b for b in mac_bytes)

def load_nodes():
    if NODES_FILE not in os.listdir():
        return {}
    try:
        with open(NODES_FILE, 'r') as f:
            return ujson.load(f)
    except Exception:
        return {}

def save_node(mac_str, node_type, name=None):
    nodes = load_nodes()
    nodes[mac_str] = {
        "node_type": node_type,
        "custom_name": name or f"{node_type}_{mac_str[-5:].replace(':', '')}",
        "paired_at": time.time()
    }
    with open(NODES_FILE, 'w') as f:
        ujson.dump(nodes, f)
    print(f"💾 Node saved to registry: {mac_str} -> {node_type}")
    return nodes[mac_str]

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

def send_espnow_msg(target_mac_str, msg_dict, routing_path=None):
    global _e
    if _e is None:
        print("❌ ESP-NOW not initialized")
        return False
        
    if routing_path is None or len(routing_path) == 0:
        routing_path = [target_mac_str]
        
    packet = {
        "msg_type": msg_dict.get("msg_type", "CMD"),
        "target_mac": target_mac_str,
        "routing_path": routing_path,
        "current_hop_index": 0,
        "payload": msg_dict.get("payload", {})
    }
    
    next_hop_mac_str = routing_path[0]
    next_hop_bytes = mac_to_bytes(next_hop_mac_str)
    
    try:
        add_peer_safe(_e, next_hop_bytes)
        payload_str = ujson.dumps(packet)
        _e.send(next_hop_bytes, payload_str.encode('utf-8'))
        print(f"🛫 ESP-NOW Sent packet to next hop {next_hop_mac_str} for target {target_mac_str}")
        return True
    except Exception as e:
        print(f"❌ Failed to send ESP-NOW packet: {e}")
        return False

def dispatch_command_from_mqtt(target_node, command, routing_path, args):
    """
    Called by mqtt_client when a command is received.
    Resolves node MAC address if node ID is passed.
    """
    nodes = load_nodes()
    target_mac_str = None
    
    if ":" in target_node:
        target_mac_str = target_node
    else:
        for mac, info in nodes.items():
            if mac == target_node or info.get("custom_name") == target_node:
                target_mac_str = mac
                break
                
    if not target_mac_str:
        print(f"⚠️ Could not resolve target node: {target_node}")
        return
        
    print(f"⚙️ Translating MQTT Command '{command}' for node {target_mac_str}")
    
    node_info = nodes.get(target_mac_str, {})
    if node_info.get("node_type") == "PUMP" and command == "PUMP_ON":
        deficit = args.get("deficit", 10.0)
        duration = args.get("duration", 300)
        scheduler.queue_irrigation(target_mac_str, deficit, duration)
    else:
        payload = {"cmd": command}
        payload.update(args)
        send_espnow_msg(target_mac_str, {
            "msg_type": "CMD",
            "payload": payload
        }, routing_path)

def espnow_receiver_thread(heartbeats=None):
    global _e
    print("🚀 ESP-NOW Master Receiver Thread Started")
    
    sta = network.WLAN(network.STA_IF)
    sta.active(True)
    
    _e = espnow.ESPNow()
    _e.active(True)
    
    cfg = config.load_config()
    client_id = cfg.get("client", {}).get("id", "hub_master_01")
    topic_prefix = cfg.get("mqtt", {}).get("topic_prefix", f"farm/{client_id}")
    
    while True:
        if heartbeats is not None:
            heartbeats["esp_now"] = time.time()
            
        try:
            host, msg = _e.recv(500)
            if host is None or msg is None:
                time.sleep_ms(50)
                continue
                
            sender_mac_str = bytes_to_mac(host)
            payload_str = msg.decode('utf-8')
            print(f"📥 ESP-NOW Raw packet from {sender_mac_str}: {payload_str}")
            
            packet = ujson.loads(payload_str)
            msg_type = packet.get("msg_type")
            target_mac = packet.get("target_mac")
            routing_path = packet.get("routing_path", [])
            current_hop_index = packet.get("current_hop_index", 0)
            payload = packet.get("payload", {})
            
            hub_mac_str = bytes_to_mac(sta.config('mac'))
            
            if target_mac != hub_mac_str:
                continue
                
            if msg_type == "PAIR_REQ":
                node_type = payload.get("node_type", "UNKNOWN")
                custom_name = payload.get("custom_name")
                print(f"🤝 PAIR_REQ received from {sender_mac_str} ({node_type})")
                
                node_info = save_node(sender_mac_str, node_type, custom_name)
                
                send_espnow_msg(sender_mac_str, {
                    "msg_type": "ACK",
                    "payload": {"status": "paired", "hub_mac": hub_mac_str}
                })
                
                mqtt_client.publish_msg(f"farm/config/new_node_added", {
                    "mac": sender_mac_str,
                    "node_type": node_type,
                    "custom_name": node_info["custom_name"]
                })
                
            elif msg_type == "TELE":
                print(f"📊 Telemetry received from node {sender_mac_str}")
                mqtt_client.publish_msg(f"{topic_prefix}/telemetry", {
                    "node_mac": sender_mac_str,
                    "data": payload
                })
                
            elif msg_type == "ACK":
                print(f"✅ ACK received from {sender_mac_str}")
                mqtt_client.publish_msg(f"{topic_prefix}/acks", {
                    "node_mac": sender_mac_str,
                    "ack_payload": payload
                })
                
            elif msg_type == "ALERT":
                print(f"🚨 ALERT received from {sender_mac_str}!")
                mqtt_client.publish_msg(f"{topic_prefix}/alerts", {
                    "node_mac": sender_mac_str,
                    "alert": payload.get("alert_type", "unknown_fault"),
                    "message": payload.get("message", "")
                })
                
        except Exception as e:
            time.sleep_ms(100)
