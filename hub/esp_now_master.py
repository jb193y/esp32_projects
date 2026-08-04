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
    print(f"💾 Node saved to registry: {mac_str} -> {node_type}")
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
        print("⚠️ ESP-NOW Peer limit reached, cleaning up oldest peer...")
        try:
            peers_list = e.peers() if callable(getattr(e, 'peers', None)) else e.peers
        except Exception:
            peers_list = []
        if peers_list:
            try:
                e.del_peer(bytes(peers_list[0][0]))
                e.add_peer(peer_bytes)
            except Exception as ex:
                print("❌ Failed to resolve peer slots:", ex)

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
    Called by mqtt_client when a command arrives on MQTT.
    Resolves node MAC address from node_id or name, then sends via ESP-NOW.
    """
    nodes = load_nodes()
    target_mac_str = None

    if ":" in target_node:
        target_mac_str = target_node
    else:
        for mac, info in nodes.items():
            if info.get("node_id") == target_node or info.get("custom_name") == target_node:
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
            target_mac = packet.get("target_mac", "")
            payload = packet.get("payload", {})

            hub_mac_str = bytes_to_mac(sta.config('mac'))

            # Accept packets targeted at us (MAC-insensitive comparison)
            if target_mac and not macs_match(target_mac, hub_mac_str):
                print(f"⚠️ Packet target {target_mac} != hub {hub_mac_str}, ignoring.")
                continue

            if msg_type == "PAIR_REQ":
                node_type = payload.get("node_type", "UNKNOWN")
                node_id = payload.get("node_id") or payload.get("custom_name", "")
                custom_name = payload.get("custom_name")
                print(f"🤝 PAIR_REQ received from {sender_mac_str} (type={node_type}, id={node_id})")

                node_info = save_node(sender_mac_str, node_type, node_id=node_id, name=custom_name)

                # ACK back to the valve
                send_espnow_msg(sender_mac_str, {
                    "msg_type": "ACK",
                    "payload": {"status": "paired", "hub_mac": hub_mac_str}
                })

                # Publish retained status so the mobile app "waiting" screen resolves
                # Topic format matches mobile app: {typeSlug}/{nodeId}/status
                type_slug = node_type.lower()
                node_id_slug = node_info.get("node_id", "").lower() or sender_mac_str.replace(':', '')
                status_topic = f"{type_slug}/{node_id_slug}/status"
                mqtt_client.publish_msg(status_topic, {
                    "status": "online",
                    "node_type": node_type,
                    "mac": sender_mac_str,
                    "timestamp": time.time()
                }, retain=True)
                print(f"📡 Published device online to: {status_topic}")

                # Also notify on discovery topic
                mqtt_client.publish_msg("farm/config/new_node_added", {
                    "mac": sender_mac_str,
                    "node_type": node_type,
                    "node_id": node_id_slug,
                    "custom_name": node_info["custom_name"]
                })

            elif msg_type == "TELE":
                print(f"📊 Telemetry received from node {sender_mac_str}")
                # Look up node info so we can publish to the correct topic
                nodes = load_nodes()
                node_info = nodes.get(sender_mac_str, {})
                node_type = node_info.get("node_type", "node").lower()
                node_id = node_info.get("node_id", sender_mac_str.replace(':', '')).lower()
                tele_topic = f"{node_type}/{node_id}/telemetry"
                tele_payload = payload.copy() if isinstance(payload, dict) else {"data": payload}
                tele_payload["node_mac"] = sender_mac_str
                mqtt_client.publish_msg(tele_topic, tele_payload)

            elif msg_type == "ACK":
                print(f"✅ ACK received from {sender_mac_str}")
                nodes = load_nodes()
                node_info = nodes.get(sender_mac_str, {})
                node_type = node_info.get("node_type", "node").lower()
                node_id = node_info.get("node_id", sender_mac_str.replace(':', '')).lower()
                mqtt_client.publish_msg(f"{node_type}/{node_id}/acks", {
                    "node_mac": sender_mac_str,
                    "ack_payload": payload
                })

            elif msg_type == "ALERT":
                print(f"🚨 ALERT received from {sender_mac_str}!")
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
            time.sleep_ms(100)
