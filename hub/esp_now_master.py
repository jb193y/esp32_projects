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

def macs_match(m1, m2):
    """Case-insensitive, colon-insensitive MAC comparison."""
    return m1.replace(':', '').lower() == m2.replace(':', '').lower()

def load_nodes():
    if NODES_FILE not in os.listdir():
        return {}
    try:
        with open(NODES_FILE, 'r') as f:
            return ujson.load(f)
    except Exception:
        return {}

def save_node(mac_str, node_type, node_id=None, name=None):
    nodes = load_nodes()
    nodes[mac_str] = {
        "node_type": node_type,
        "node_id": node_id or mac_str.replace(':', '')[-8:],
        "custom_name": name or "{}_{}".format(node_type, mac_str[-5:].replace(':', '')),
        "paired_at": time.time()
    }
    with open(NODES_FILE, 'w') as f:
        ujson.dump(nodes, f)
    print(f" Node saved to registry: {mac_str} -> {node_type}")
    return nodes[mac_str]

def add_peer_safe(e, peer_bytes):
    """Add ESP-NOW peer, removing oldest if peer table is full."""
    peer_bytes = bytes(peer_bytes)
    try:
        peers_list = e.peers() if callable(getattr(e, 'peers', None)) else e.peers
        peer_macs = [bytes(p[0]) for p in peers_list]
    except Exception:
        peer_macs = []

    if peer_bytes in peer_macs:
        return  # already registered

    try:
        e.add_peer(peer_bytes)
    except OSError:
        print("ESP-NOW Peer limit reached, cleaning up oldest peer...")
        try:
            peers_list = e.peers() if callable(getattr(e, 'peers', None)) else e.peers
        except Exception:
            peers_list = []
        if peers_list:
            try:
                e.del_peer(bytes(peers_list[0][0]))
                e.add_peer(peer_bytes)
            except Exception as ex:
                print("Failed to resolve peer slots:", ex)

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

def send_espnow_msg(target_mac_str, msg_dict, routing_path=None):
    global _e
    if _e is None:
        print("ESP-NOW not initialized")
        return False

    if not routing_path:
        routing_path = [target_mac_str]

    packet = {
        "t": msg_dict.get("msg_type", "CMD"),
        "dst": target_mac_str,
        "pld": msg_dict.get("payload", {})
    }
    if len(routing_path) > 1:
        packet["path"] = routing_path
        packet["hop"] = 0

    next_hop_mac_str = routing_path[0]
    next_hop_bytes = mac_to_bytes(next_hop_mac_str)

    try:
        add_peer_safe(_e, next_hop_bytes)
        payload_str = ujson.dumps(packet)
        try:
            _e.send(next_hop_bytes, payload_str.encode('utf-8'))
            print(" ESP-NOW message sent to", target_mac_str)
            return True
        except Exception as send_err:
            print(" ESP-NOW send notice:", send_err)
            return False
    except Exception as e:
        print(" Failed to send ESP-NOW packet:", e)
        return False

_discovery_active_until = 0

def start_mesh_discovery(duration_sec=60):
    global _discovery_active_until
    _discovery_active_until = time.time() + duration_sec
    print(f" Mesh Discovery Mode STARTED for {duration_sec} seconds!")

def dispatch_command_from_mqtt(target_node, command, routing_path, args):
    """
    Called by mqtt_client when a command arrives on MQTT.
    Resolves node MAC address from node_id or name, then sends via ESP-NOW.
    """
    if command in ("START_DISCOVERY", "START_MESH_DISCOVERY"):
        duration = args.get("duration_sec", 60) if isinstance(args, dict) else 60
        start_mesh_discovery(duration)
        cfg = config.load_config()
        client_id = cfg.get("client", {}).get("id", "hub_master_01")
        resp = {
            "status": "DISCOVERY_STARTED",
            "duration_sec": duration,
            "timestamp": time.time(),
            "hub_id": client_id
        }
        mqtt_client.publish_msg(f"farm/{client_id}/discovery_status", resp)
        print(f" Discovery Mode triggered via MQTT for {duration} seconds.")
        return

    nodes = load_nodes()
    cfg = config.load_config()
    client_id = cfg.get("client", {}).get("id", "hub_master_01")

    # Support target_node == "all" to send commands to all registered nodes
    if target_node.lower() in ("all", "broadcast", "*"):
        print(f" Broadcast Command '{command}' to ALL {len(nodes)} registered nodes")
        fwd_payload = {
            "status": "FORWARDING_TO_ALL",
            "target_node": "all",
            "command": command,
            "timestamp": time.time(),
            "hub_id": client_id
        }
        mqtt_client.publish_msg(f"farm/{client_id}/command_response", fwd_payload)

        payload = {"cmd": command}
        payload.update(args)

        if len(nodes) == 0:
            # Fallback to ESP-NOW hardware broadcast if nodes registry is empty
            send_espnow_msg("ff:ff:ff:ff:ff:ff", {"msg_type": "CMD", "payload": payload})
        else:
            for mac_addr in nodes.keys():
                send_espnow_msg(mac_addr, {"msg_type": "CMD", "payload": payload})
        return

    target_mac_str = None
    if ":" in target_node:
        target_mac_str = target_node
    else:
        for mac, info in nodes.items():
            if info.get("node_id") == target_node or info.get("custom_name") == target_node:
                target_mac_str = mac
                break

    if not target_mac_str:
        print(f" Could not resolve target node: {target_node}")
        return

    print(f" Translating MQTT Command '{command}' for node {target_mac_str}")

    cfg = config.load_config()
    client_id = cfg.get("client", {}).get("id", "hub_master_01")
    node_info = nodes.get(target_mac_str, {})
    node_type = node_info.get("node_type", "node").lower()
    node_id_slug = node_info.get("node_id", target_mac_str.replace(':', '')).lower()

    # Publish FORWARDING_TO_NODE response
    fwd_payload = {
        "status": "FORWARDING_TO_NODE",
        "target_node": target_node,
        "node_mac": target_mac_str,
        "command": command,
        "timestamp": time.time(),
        "hub_id": client_id
    }
    mqtt_client.publish_msg(f"{node_type}/{node_id_slug}/command/response", fwd_payload)
    mqtt_client.publish_msg(f"farm/{client_id}/command_response", fwd_payload)

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
    print(" ESP-NOW Master Receiver Thread Started")

    sta = network.WLAN(network.STA_IF)

    _e = espnow.ESPNow()
    _e.active(True)

    cfg = config.load_config()
    client_id = cfg.get("client", {}).get("id", "hub_master_01")
    topic_prefix = cfg.get("mqtt", {}).get("topic_prefix", f"farm/{client_id}")

    _last_beacon_time = 0

    while True:
        if heartbeats is not None:
            heartbeats["esp_now"] = time.time()

        now = time.time()
        if now < _discovery_active_until:
            if now - _last_beacon_time >= 1.5:
                _last_beacon_time = now
                try:
                    active_ch = sta.config('channel')
                except Exception:
                    active_ch = 4
                hub_mac_str = bytes_to_mac(sta.config('mac'))
                beacon_pkt = {
                    "msg_type": "BEACON",
                    "target_mac": "ff:ff:ff:ff:ff:ff",
                    "routing_path": ["ff:ff:ff:ff:ff:ff"],
                    "current_hop_index": 0,
                    "payload": {
                        "hub_mac": hub_mac_str,
                        "sender_mac": hub_mac_str,
                        "hop_count": 0,
                        "channel": active_ch
                    }
                }
                try:
                    add_peer_safe(_e, b'\xff\xff\xff\xff\xff\xff')
                    _e.send(b'\xff\xff\xff\xff\xff\xff', ujson.dumps(beacon_pkt).encode('utf-8'))
                    print(f" Sent Discovery BEACON on Channel {active_ch}")
                except Exception as b_err:
                    print(" Beacon send notice:", b_err)

        try:
            host, msg = _e.recv(500)
            if not host or not msg:
                time.sleep_ms(50)
                continue

            sender_mac_str = bytes_to_mac(host)
            payload_str = msg.decode('utf-8')
            print(f"ESP-NOW Raw packet from {sender_mac_str}: {payload_str}")

            packet = parse_packet(payload_str)
            msg_type = packet.get("msg_type")
            target_mac = packet.get("target_mac", "")
            payload = packet.get("payload", {})

            hub_mac_str = bytes_to_mac(sta.config('mac'))

            # Accept packets targeted at us or broadcast MACs
            is_broadcast = target_mac in ("00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff", "FF:FF:FF:FF:FF:FF")
            if target_mac and not is_broadcast and not macs_match(target_mac, hub_mac_str):
                print(f"Packet target {target_mac} != hub {hub_mac_str}, ignoring.")
                continue

            if msg_type == "PAIR_REQ":
                node_type = payload.get("node_type", "UNKNOWN")
                node_id = payload.get("node_id") or payload.get("custom_name", "")
                custom_name = payload.get("custom_name")
                print(f"PAIR_REQ received from {sender_mac_str} (type={node_type}, id={node_id})")

                node_info = save_node(sender_mac_str, node_type, node_id=node_id, name=custom_name)

                active_ch = 6
                try:
                    active_ch = sta.config('channel')
                except Exception:
                    pass

                # ACK back to the valve
                send_espnow_msg(sender_mac_str, {
                    "msg_type": "ACK",
                    "payload": {"status": "paired", "hub_mac": hub_mac_str, "channel": active_ch}
                })

                # Publish retained status so the mobile app "waiting" screen resolves
                type_slug = node_type.lower()
                node_id_slug = node_info.get("node_id", "").lower() or sender_mac_str.replace(':', '')

                status_payload = {
                    "status": "online",
                    "node_type": node_type,
                    "mac": sender_mac_str,
                    "timestamp": time.time()
                }

                mqtt_client.publish_msg(f"{type_slug}/{node_id_slug}/status", status_payload, retain=True)
                mqtt_client.publish_msg(f"{topic_prefix}/{type_slug}/{node_id_slug}/status", status_payload, retain=True)
                print(f"Published device online status for {node_id_slug}")

                # Also notify on discovery topic
                mqtt_client.publish_msg("farm/config/new_node_added", {
                    "mac": sender_mac_str,
                    "node_type": node_type,
                    "node_id": node_id_slug,
                    "custom_name": node_info["custom_name"]
                })

            elif msg_type == "TELE":
                print(f"Telemetry received from node {sender_mac_str}")
                nodes = load_nodes()
                node_info = nodes.get(sender_mac_str, {})
                node_type = node_info.get("node_type", "node").lower()
                node_id = node_info.get("node_id", sender_mac_str.replace(':', '')).lower()
                tele_topic = f"{node_type}/{node_id}/telemetry"
                tele_payload = payload.copy() if isinstance(payload, dict) else {"data": payload}
                tele_payload["node_mac"] = sender_mac_str
                mqtt_client.publish_msg(tele_topic, tele_payload)

            elif msg_type == "ACK":
                print(f"ACK received from {sender_mac_str}")
                nodes = load_nodes()
                node_info = nodes.get(sender_mac_str, {})
                node_type = node_info.get("node_type", "node").lower()
                node_id = node_info.get("node_id", sender_mac_str.replace(':', '')).lower()
                
                exec_payload = {
                    "status": "EXECUTED_BY_NODE",
                    "target_node": node_id,
                    "node_mac": sender_mac_str,
                    "ack_data": payload,
                    "timestamp": time.time()
                }
                mqtt_client.publish_msg(f"{node_type}/{node_id}/command/response", exec_payload)
                mqtt_client.publish_msg(f"{node_type}/{node_id}/acks", exec_payload)
                mqtt_client.publish_msg(f"farm/{client_id}/command_response", exec_payload)
                print(f"Published EXECUTED_BY_NODE ACK for {node_id}")

            elif msg_type == "ALERT":
                print(f"ALERT received from {sender_mac_str}!")
                nodes = load_nodes()
                node_info = nodes.get(sender_mac_str, {})
                node_type = node_info.get("node_type", "node").lower()
                node_id = node_info.get("node_id", sender_mac_str.replace(':', '')).lower()
                mqtt_client.publish_msg(f"{node_type}/{node_id}/alerts", {
                    "node_mac": sender_mac_str,
                    "alert": payload.get("alert_type", "unknown_fault"),
                    "message": payload.get("message", "")
                })

        except Exception as e:
            err_str = str(e)
            if "buffer error" not in err_str:
                print(f"Error in espnow_receiver_thread loop: {e}")
            time.sleep_ms(100)
