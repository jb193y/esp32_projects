# esp_now_client.py (Shared ESP-NOW Client Library)
import network
try:
    import espnow
    has_espnow = True
except ImportError:
    espnow = None
    has_espnow = False

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
        print(" ESP-NOW Peer limit reached, cleaning up...")
        try:
            peers_list = e.peers() if callable(getattr(e, 'peers', None)) else e.peers
        except:
            peers_list = []
        if peers_list:
            try:
                e.del_peer(peers_list[0][0])
                e.add_peer(peer_bytes)
            except Exception as ex:
                print(" Failed to resolve peer slots:", ex)
                raise err

def parse_packet(payload_str):
    p = ujson.loads(payload_str)
    msg_type = p.get("msg_type") or p.get("t")
    target = p.get("target") or p.get("dst", "")
    source = p.get("source")
    route = p.get("route", {})
    hops = route.get("hops", p.get("routing_path", p.get("path", [])))
    current_hop_index = route.get("current_hop_index", p.get("current_hop_index", p.get("hop", 0)))
    data = p.get("data", p.get("payload", p.get("pld", {})))
    return {
        "msg_type": msg_type,
        "target": target,
        "source": source,
        "route": route,
        "hops": hops,
        "current_hop_index": current_hop_index,
        "data": data,
        "raw": p
    }

def get_route_for_target(target_id, target_mac=None):
    cfg = config.load_config()
    
    # Check if there is a pre-provisioned route in config for this target
    routes = cfg.get("routes", {})
    if target_id in routes:
        route_info = routes[target_id]
        return route_info.get("route_id", "pre_provisioned"), route_info.get("hops", [])
        
    # Dynamic fallback to parent/hub configuration
    hub_cfg = cfg.get("hub", {})
    hub_mac = hub_cfg.get("mac", "ff:ff:ff:ff:ff:ff")
    
    parent_cfg = cfg.get("parent", {})
    parent_mac = parent_cfg.get("mac", "00:00:00:00:00:00")
    
    dest_mac = target_mac or hub_mac
    
    hops = []
    if parent_mac != "00:00:00:00:00:00" and parent_mac != "ff:ff:ff:ff:ff:ff":
        hops.append(parent_mac)
    if dest_mac != "00:00:00:00:00:00" and dest_mac not in hops:
        hops.append(dest_mac)
        
    if not hops:
        hops = [dest_mac]
        
    return "dynamic_fallback", hops

def send_ack_or_tele_to_hub(msg_type, payload, target_mac=None):
    global _e
    if _e is None:
        return False

    cfg = config.load_config()
    client_cfg = cfg.get("client", {})
    source_id = client_cfg.get("id", "unknown_node")

    # If it is status pairing request or target_mac is broadcast, target is broadcast
    is_broadcast = (target_mac == "ff:ff:ff:ff:ff:ff" or msg_type == "PAIR_REQ" or (msg_type == "STATUS" and payload.get("status") == "pairing_request"))
    target_id = "broadcast" if is_broadcast else "hub_master_01"

    # Get pre-provisioned route or dynamic fallback
    route_id, hops = get_route_for_target(target_id, target_mac)

    envelope = {
        "source": source_id,
        "target": target_id,
        "msg_type": "STATUS" if msg_type == "PAIR_REQ" else msg_type,
        "timestamp": int(time.time()),
        "route": {
            "transport": "ESPNOW",
            "route_id": route_id,
            "current_hop_index": 0,
            "hops": hops,
            "link_diagnostics": []
        },
        "data": payload
    }

    next_hop = hops[0]
    next_hop_bytes = mac_to_bytes(next_hop)

    try:
        add_peer_safe(_e, next_hop_bytes)
        payload_str = ujson.dumps(envelope)
        try:
            _e.send(next_hop_bytes, payload_str.encode('utf-8'))
            print(f" Envelope sent to next hop {next_hop} for destination {target_id}")
            return True
        except Exception as send_err:
            print(f" ESP-NOW send notice to {next_hop}: {send_err}")
            return False
    except Exception as err:
        print(f" Failed to transmit packet to destination {target_id}:", err)
        return False

# Backward compatibility alias for pump controller
def send_to_hub(msg_type, payload):
    return send_ack_or_tele_to_hub(msg_type, payload)

_pair_channel_idx = 0

def send_pairing_request():
    global _e, _pair_channel_idx
    if _e is None:
        return

    channels = [4, 6, 1, 11]
    ch = channels[_pair_channel_idx % len(channels)]
    _pair_channel_idx += 1
    
    sta = network.WLAN(network.STA_IF)
    try:
        sta.config(channel=ch)
    except Exception:
        pass

    cfg = config.load_config()
    client_cfg = cfg.get("client", {})
    node_type = client_cfg.get("type", "client").upper()
    payload = {
        "status": "pairing_request",
        "node_type": node_type,
        "node_id": client_cfg.get("id", ""),
        "custom_name": client_cfg.get("custom_name", "Client Node")
    }
    print(f"Sending PAIR_REQ from {node_type} to broadcast on Channel {ch}...")
    send_ack_or_tele_to_hub("STATUS", payload, target_mac="ff:ff:ff:ff:ff:ff")


def init_espnow_client(on_cmd_received_fn=None):
    global _e
    if not has_espnow:
        print(" ESP-NOW not supported on this firmware build.")
        return None

    cfg = config.load_config()
    client_name = cfg.get("client", {}).get("custom_name", "Client Node")
    print(f" Initializing {client_name} ESP-NOW Client...")
    
    sta = network.WLAN(network.STA_IF)
    sta.active(True)
    ch = cfg.get("wifi", {}).get("channel", 4)
    try:
        sta.config(channel=ch)
    except Exception:
        pass
    
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
    
    cfg = config.load_config()
    client_cfg = cfg.get("client", {})
    local_id = client_cfg.get("id", "").lower()
    
    while True:
        if heartbeats is not None:
            heartbeats["esp_now"] = time.time()
            
        try:
            host, msg = _e.recv(200)
            if host and msg:
                sender_mac = bytes_to_mac(host)
                payload_str = msg.decode('utf-8')
                print(f" Received packet from {sender_mac}: {payload_str}")
                
                packet = parse_packet(payload_str)
                msg_type = packet.get("msg_type")
                target = packet.get("target")
                
                is_for_us = False
                if target:
                    t_lower = target.lower()
                    is_for_us = (t_lower == local_id or t_lower in ("broadcast", "ff:ff:ff:ff:ff:ff"))
                else:
                    is_for_us = (msg_type == "CMD" or msg_type == "ACK" or msg_type == "COMMAND")

                if msg_type == "BEACON" and not _paired:
                    b_pld = packet.get("data", {})
                    hub_mac = b_pld.get("hub_mac", sender_mac)
                    parent_mac = b_pld.get("sender_mac", sender_mac)
                    b_ch = b_pld.get("channel")
                    print(f" Received Discovery BEACON from {parent_mac} (Hub: {hub_mac}, Channel: {b_ch})")
                    try:
                        upd = {"hub": {"mac": hub_mac}, "parent": {"mac": parent_mac}}
                        if b_ch:
                            upd["wifi"] = {"channel": b_ch}
                            sta.config(channel=b_ch)
                        config.update_config(upd)
                        # Immediately send pairing STATUS to chosen parent
                        client_cfg = config.load_config().get("client", {})
                        pld = {
                            "status": "pairing_request",
                            "node_type": client_cfg.get("type", "VALVE").upper(),
                            "node_id": client_cfg.get("id", ""),
                            "custom_name": client_cfg.get("custom_name", "Client Node")
                        }
                        send_ack_or_tele_to_hub("STATUS", pld, target_mac=parent_mac)
                    except Exception as b_ex:
                        print("Error handling discovery BEACON:", b_ex)

                if msg_type == "ACK" and is_for_us:
                    ack_pld = packet.get("data", {})
                    if ack_pld.get("status") == "paired":
                        _paired = True
                        hub_mac = ack_pld.get("hub_mac", sender_mac)
                        hub_ch = ack_pld.get("channel")
                        print(f"Client paired successfully with Hub ({hub_mac}) on Channel {hub_ch}!")
                        try:
                            upd = {"hub": {"mac": hub_mac}, "parent": {"mac": hub_mac}}
                            if hub_ch:
                                upd["wifi"] = {"channel": hub_ch}
                                sta = network.WLAN(network.STA_IF)
                                sta.config(channel=hub_ch)
                            config.update_config(upd)
                        except Exception as ex:
                            print("Error updating config on pairing:", ex)
                
                # Relaying and target validation
                is_actually_for_us = relay_engine.process_and_relay(packet)
                    
                if is_actually_for_us and (msg_type == "COMMAND" or msg_type == "CMD") and on_cmd_received_fn is not None:
                    payload = packet.get("data", {})
                    cmd = payload.get("cmd") or payload.get("command")
                    
                    payload["sender_mac"] = sender_mac
                    payload["routing_path"] = packet.get("hops", [])
                    
                    on_cmd_received_fn(cmd, payload)
            else:
                if not _paired:
                    send_pairing_request()
                    time.sleep(5)
                else:
                    time.sleep_ms(50)
                    
        except Exception as err:
            err_str = str(err)
            if "buffer error" not in err_str:
                print("Client loop error:", err)
            time.sleep_ms(100)
