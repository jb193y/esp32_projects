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
        "paired_at": config.get_unix_time()
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

def send_espnow_msg(target_mac_str, msg_dict, routing_path=None, target_id=None):
    global _e
    if _e is None:
        print("ESP-NOW not initialized")
        return False

    cfg = config.load_config()
    hub_id = cfg.get("client", {}).get("id", "hub_master_01")

    if not routing_path:
        routing_path = [target_mac_str]

    msg_type = msg_dict.get("msg_type", "COMMAND")
    if msg_type == "CMD":
        msg_type = "COMMAND"
    data_payload = msg_dict.get("payload", {})

    envelope = {
        "source": hub_id,
        "target": target_id or target_mac_str,
        "msg_type": msg_type,
        "timestamp": int(config.get_unix_time()),
        "route": {
            "route_id": "to_node",
            "hops": routing_path
        },
        "data": data_payload
    }

    next_hop_mac_str = routing_path[0]
    next_hop_bytes = mac_to_bytes(next_hop_mac_str)

    try:
        add_peer_safe(_e, next_hop_bytes)
        payload_str = ujson.dumps(envelope)
        try:
            _e.send(next_hop_bytes, payload_str.encode('utf-8'))
            print(" ESP-NOW message sent to next hop", next_hop_mac_str)
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
    _discovery_active_until = config.get_unix_time() + duration_sec
    print(f" Mesh Discovery Mode STARTED for {duration_sec} seconds!")

def dispatch_command_from_mqtt(target_node, command, routing_path, args):
    """
    Called by mqtt_client when a command arrives on MQTT.
    Resolves node MAC address from node_id or name, then sends via ESP-NOW.
    """
    cfg = config.load_config()
    client_id = cfg.get("client", {}).get("id", "hub_master_01")

    if command in ("START_DISCOVERY", "START_MESH_DISCOVERY"):
        duration = args.get("duration_sec", 60) if isinstance(args, dict) else 60
        start_mesh_discovery(duration)
        resp = {
            "source": client_id,
            "target": "backend_api",
            "msg_type": "ACK",
            "timestamp": config.get_unix_time(),
            "route": {
                "transport": "MQTT",
                "route_id": "discovery_ack",
                "current_hop_index": 0,
                "hops": ["backend_api"],
                "link_diagnostics": []
            },
            "data": {
                "status": "DISCOVERY_STARTED",
                "duration_sec": duration,
                "hub_id": client_id
            }
        }
        mqtt_client.publish_msg(f"farm/{client_id}/discovery_status", resp)
        print(f" Discovery Mode triggered via MQTT for {duration} seconds.")
        return

    nodes = load_nodes()

    # Support target_node == "all" to send commands to all registered nodes
    if target_node.lower() in ("all", "broadcast", "*"):
        print(f" Broadcast Command '{command}' to ALL {len(nodes)} registered nodes")
        fwd_payload = {
            "source": client_id,
            "target": "backend_api",
            "msg_type": "ACK",
            "timestamp": config.get_unix_time(),
            "route": {
                "transport": "MQTT",
                "route_id": "broadcast_fwd_ack",
                "current_hop_index": 0,
                "hops": ["backend_api"],
                "link_diagnostics": []
            },
            "data": {
                "status": "FORWARDING_TO_ALL",
                "target_node": "all",
                "command": command,
                "hub_id": client_id
            }
        }
        mqtt_client.publish_msg(f"farm/{client_id}/command_response", fwd_payload)

        payload = {"cmd": command}
        payload.update(args)

        if len(nodes) == 0:
            # Fallback to ESP-NOW hardware broadcast if nodes registry is empty
            send_espnow_msg("ff:ff:ff:ff:ff:ff", {"msg_type": "COMMAND", "payload": payload}, target_id="broadcast")
        else:
            for mac_addr in nodes.keys():
                send_espnow_msg(mac_addr, {"msg_type": "COMMAND", "payload": payload}, target_id=nodes[mac_addr].get("node_id", "broadcast"))
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

    node_info = nodes.get(target_mac_str, {})
    node_type = node_info.get("node_type", "node").lower()
    node_id_slug = node_info.get("node_id", target_mac_str.replace(':', '')).lower()

    # Publish FORWARDING_TO_NODE response to MQTT in standard envelope
    fwd_payload = {
        "source": client_id,
        "target": "backend_api",
        "msg_type": "ACK",
        "timestamp": config.get_unix_time(),
        "route": {
            "transport": "MQTT",
            "route_id": "fwd_cmd_ack",
            "current_hop_index": 0,
            "hops": ["backend_api"],
            "link_diagnostics": []
        },
        "data": {
            "status": "FORWARDING_TO_NODE",
            "target_node": target_node,
            "node_mac": target_mac_str,
            "command": command,
            "hub_id": client_id
        }
    }
    mqtt_client.publish_msg(f"{node_type}/{node_id_slug}/command/response", fwd_payload)
    mqtt_client.publish_msg(f"farm/{client_id}/command_response", fwd_payload)

    # Translate logical node IDs in routing_path to MAC addresses
    mac_routing_path = []
    if routing_path:
        for hop in routing_path:
            if ":" in hop:
                mac_routing_path.append(hop)
            else:
                hop_mac = None
                for mac, info in nodes.items():
                    if info.get("node_id") == hop or info.get("custom_name") == hop:
                        hop_mac = mac
                        break
                if hop_mac:
                    mac_routing_path.append(hop_mac)

    if not mac_routing_path:
        mac_routing_path = [target_mac_str]

    if node_info.get("node_type") == "PUMP" and command == "PUMP_ON":
        deficit = args.get("deficit", 10.0)
        duration = args.get("duration", 300)
        scheduler.queue_irrigation(target_mac_str, deficit, duration)
    else:
        payload = {"cmd": command}
        payload.update(args)
        send_espnow_msg(target_mac_str, {
            "msg_type": "COMMAND",
            "payload": payload
        }, mac_routing_path, target_id=target_node)

def espnow_receiver_thread(heartbeats=None):
    global _e
    print(" ESP-NOW Master Receiver Thread Started")

    sta = network.WLAN(network.STA_IF)

    _e = espnow.ESPNow()
    _e.active(True)

    # Explicitly register broadcast peer so MicroPython delivers full broadcast packets
    add_peer_safe(_e, b'\xff\xff\xff\xff\xff\xff')

    _last_beacon_time = 0

    while True:
        if heartbeats is not None:
            heartbeats["esp_now"] = config.get_unix_time()

        now = config.get_unix_time()
        if now < _discovery_active_until:
            if now - _last_beacon_time >= 1.5:
                _last_beacon_time = now
                try:
                    active_ch = sta.config('channel')
                except Exception:
                    active_ch = 4
                hub_mac_str = bytes_to_mac(sta.config('mac'))
                beacon_pkt = {
                    "source": "hub_master_01",
                    "target": "broadcast",
                    "msg_type": "BEACON",
                    "timestamp": int(config.get_unix_time()),
                    "route": {
                        "transport": "ESPNOW",
                        "route_id": "beacon",
                        "current_hop_index": 0,
                        "hops": ["ff:ff:ff:ff:ff:ff"],
                        "link_diagnostics": []
                    },
                    "data": {
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
            target = packet.get("target", "")
            source = packet.get("source")
            data = packet.get("data", {})

            cfg = config.load_config()
            client_id = cfg.get("client", {}).get("id", "hub_master_01")
            hub_mac_str = bytes_to_mac(sta.config('mac'))

            # Accept packets targeted at us, broadcast, or if target matches our hub ID
            is_broadcast = target in ("00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff", "FF:FF:FF:FF:FF:FF", "broadcast")
            is_for_us = is_broadcast or (target and target.lower() == client_id.lower()) or (target and target.upper() == hub_mac_str.upper())
            
            if not is_for_us:
                print(f"Packet target {target} != hub {client_id}, ignoring.")
                continue

            if msg_type == "PAIR_REQ" or (msg_type == "STATUS" and data.get("status") == "pairing_request"):
                node_type = data.get("node_type", "UNKNOWN")
                node_id = source or data.get("node_id") or data.get("custom_name", "")
                custom_name = data.get("custom_name")
                print(f"PAIR_REQ received from {sender_mac_str} (type={node_type}, id={node_id})")

                node_info = save_node(sender_mac_str, node_type, node_id=node_id, name=custom_name)

                active_ch = 6
                try:
                    active_ch = sta.config('channel')
                except Exception:
                    pass

                # ACK back to the node
                send_espnow_msg(sender_mac_str, {
                    "msg_type": "ACK",
                    "payload": {"status": "paired", "hub_mac": hub_mac_str, "channel": active_ch}
                }, target_id=node_id)

                # Publish retained status so the mobile app "waiting" screen resolves
                type_slug = node_type.lower()
                device_id = node_info.get("node_id", "").lower() or sender_mac_str.replace(':', '')

                site = cfg.get("client", {}).get("site", "default_site")
                group = cfg.get("client", {}).get("group", "all")

                if site == "default_site":
                    print("ERROR: 'site' not set in hub config. Cannot publish node status to MQTT.")
                    continue

                status_payload = {
                    "source": device_id,
                    "target": "backend_api",
                    "msg_type": "STATUS",
                    "timestamp": config.get_unix_time(),
                    "route": {
                        "transport": "ESPNOW",
                        "route_id": packet.get("route", {}).get("route_id", "direct"),
                        "current_hop_index": packet.get("current_hop_index", 0),
                        "hops": packet.get("hops", []),
                        "link_diagnostics": []
                    },
                    "data": {
                        "device_id": device_id,
                        "status": "online",
                        "node_type": node_type,
                        "mac": sender_mac_str
                    }
                }
                
                # Publish to new namespaced topic only
                status_topic = f"{site}/{group}/{type_slug}/{device_id}/status"
                mqtt_client.publish_msg(status_topic, status_payload, retain=True)
                print(f"Published device online status for {device_id}")

                # Also notify on discovery topic
                new_node_payload = {
                    "source": "hub_master_01",
                    "target": "backend_api",
                    "msg_type": "STATUS",
                    "timestamp": config.get_unix_time(),
                    "route": {
                        "transport": "MQTT",
                        "route_id": "hub_discovery_notice",
                        "current_hop_index": 0,
                        "hops": ["backend_api"],
                        "link_diagnostics": []
                    },
                    "data": {
                        "mac": sender_mac_str,
                        "device_type": node_type,
                        "device_id": device_id,
                        "custom_name": node_info["custom_name"]
                    }
                }
                mqtt_client.publish_msg("farm/config/new_node_added", new_node_payload)

            elif msg_type in ("TELE", "TELEMETRY"):
                print(f"Telemetry received from node {sender_mac_str}")
                site = cfg.get("client", {}).get("site", "default_site")
                group = cfg.get("client", {}).get("group", "all")

                if site == "default_site":
                    print("ERROR: 'site' not set in hub config. Cannot publish node telemetry.")
                    continue

                nodes = load_nodes()
                node_info = nodes.get(sender_mac_str, {})
                node_type = node_info.get("node_type", "node").lower()
                device_id = node_info.get("node_id", sender_mac_str.replace(':', '')).lower()
                
                tele_topic = f"{site}/{group}/{node_type}/{device_id}/telemetry"
                tele_payload = data.copy() if isinstance(data, dict) else {"data": data}
                tele_payload["device_id"] = device_id
                tele_payload["node_mac"] = sender_mac_str

                # Wrap in standard JSON envelope before publishing to MQTT
                mqtt_payload = {
                    "source": source or device_id,
                    "target": "hub_master_01",
                    "msg_type": "TELEMETRY",
                    "timestamp": packet.get("raw", {}).get("timestamp", int(config.get_unix_time())),
                    "route": {
                        "transport": "ESPNOW",
                        "route_id": packet.get("route", {}).get("route_id", "direct"),
                        "current_hop_index": packet.get("current_hop_index", 0),
                        "hops": packet.get("hops", []),
                        "link_diagnostics": []
                    },
                    "data": tele_payload
                }
                mqtt_client.publish_msg(tele_topic, mqtt_payload)

            elif msg_type == "ACK":
                print(f"ACK received from {sender_mac_str}")
                nodes = load_nodes()
                site = cfg.get("client", {}).get("site", "default_site")
                group = cfg.get("client", {}).get("group", "all")

                if site == "default_site":
                    print("ERROR: 'site' not set in hub config. Cannot publish node ACK.")
                    continue

                node_info = nodes.get(sender_mac_str, {})
                node_type = node_info.get("node_type", "node").lower()
                device_id = node_info.get("node_id", sender_mac_str.replace(':', '')).lower()
                
                exec_payload = {
                    "status": "EXECUTED_BY_NODE",
                    "device_id": device_id,
                    "node_mac": sender_mac_str,
                    "ack_data": data,
                    "timestamp": config.get_unix_time()
                }

                # Wrap in standard JSON envelope before publishing to MQTT
                mqtt_payload = {
                    "source": source or device_id,
                    "target": "hub_master_01",
                    "msg_type": "ACK",
                    "timestamp": packet.get("raw", {}).get("timestamp", int(config.get_unix_time())),
                    "route": {
                        "transport": "ESPNOW",
                        "route_id": packet.get("route", {}).get("route_id", "direct"),
                        "current_hop_index": packet.get("current_hop_index", 0),
                        "hops": packet.get("hops", []),
                        "link_diagnostics": []
                    },
                    "data": exec_payload
                }
                
                # Publish to new namespaced topics
                mqtt_client.publish_msg(f"{site}/{group}/{node_type}/{device_id}/command/response", mqtt_payload)
                mqtt_client.publish_msg(f"{site}/{group}/{node_type}/{device_id}/acks", mqtt_payload)
                mqtt_client.publish_msg(f"farm/{client_id}/command_response", mqtt_payload)
                print(f"Published EXECUTED_BY_NODE ACK for {device_id}")

            elif msg_type in ("ALERT", "ALERTS"):
                print(f"ALERT received from {sender_mac_str}!")
                nodes = load_nodes()
                site = cfg.get("client", {}).get("site", "default_site")
                group = cfg.get("client", {}).get("group", "all")

                if site == "default_site":
                    print("ERROR: 'site' not set in hub config. Cannot publish node alert.")
                    continue

                node_info = nodes.get(sender_mac_str, {})
                node_type = node_info.get("node_type", "node").lower()
                device_id = node_info.get("node_id", sender_mac_str.replace(':', '')).lower()
                
                alert_topic = f"{site}/{group}/{node_type}/{device_id}/alerts"
                alert_payload = data.copy() if isinstance(data, dict) else {"data": data}
                alert_payload["device_id"] = device_id
                alert_payload["device_type"] = node_type.upper()
                alert_payload["node_mac"] = sender_mac_str

                # Wrap in standard JSON envelope before publishing to MQTT
                mqtt_payload = {
                    "source": source or device_id,
                    "target": "hub_master_01",
                    "msg_type": "ALERT",
                    "timestamp": packet.get("raw", {}).get("timestamp", int(config.get_unix_time())),
                    "route": {
                        "transport": "ESPNOW",
                        "route_id": packet.get("route", {}).get("route_id", "direct"),
                        "current_hop_index": packet.get("current_hop_index", 0),
                        "hops": packet.get("hops", []),
                        "link_diagnostics": []
                    },
                    "data": alert_payload
                }
                mqtt_client.publish_msg(alert_topic, mqtt_payload)

        except Exception as e:
            err_str = str(e)
            if "buffer error" not in err_str:
                print(f"Error in espnow_receiver_thread loop: {e}")
            time.sleep_ms(100)
