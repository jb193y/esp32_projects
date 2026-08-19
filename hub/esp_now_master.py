# esp_now_master.py (Hub)
import network
import espnow
import ujson
import time
import ubinascii
import os
import gc
import config
import mqtt_client
import scheduler

_e = None
NODES_FILE = "nodes.json"
# Per-sender receive buffers to assemble fragmented ESP-NOW packets
recv_buffers = {}
recv_last_seen = {}
MAX_RX_BUFFER = 2048

def _extract_json_objects_from_bytes(b):
    """Return (list_of_byte_objects, remainder_bytes).
    Uses a simple state machine to find balanced top-level JSON objects.
    Handles string escapes so braces inside strings don't break parsing.
    """
    objs = []
    start = None
    depth = 0
    in_str = False
    esc = False
    last_end = 0
    for i in range(len(b)):
        ch = chr(b[i])
        if esc:
            esc = False
            continue
        if ch == '\\':
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == '{':
            if depth == 0:
                start = i
            depth += 1
        elif ch == '}':
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    objs.append(b[start:i+1])
                    last_end = i+1
                    start = None

    remainder = b[last_end:]
    return objs, remainder

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

def add_peer_safe(e, peer_bytes, channel=0):
    """Add/update an ESP-NOW peer. add_peer is idempotent."""
    peer_bytes = bytes(peer_bytes)
    try:
        e.add_peer(peer_bytes, b'', channel, network.STA_IF)
    except OSError as ose:
        # Ignore 'ESP-NOW peer already exists' error, which is expected.
        if ose.args[0] != 23:
            print(f"add_peer_safe notice: {ose}")

def parse_packet(payload_str):
    p = ujson.loads(payload_str)
    msg_type = p.get("msg_type") or p.get("t")
    target = p.get("target") or p.get("dst", "")
    source = p.get("source") or p.get("src")
    
    route = p.get("route") or p.get("rt", {})
    hops = route.get("hops") if isinstance(route, dict) else []
    if not hops:
        hops = p.get("routing_path") or p.get("path") or []
        
    current_hop_index = route.get("current_hop_index") if isinstance(route, dict) else 0
    if current_hop_index == 0:
        current_hop_index = p.get("current_hop_index") or p.get("hop", 0)
        
    data = p.get("data") or p.get("payload") or p.get("pld", {})
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
        "src": hub_id,
        "dst": target_id or target_mac_str,
        "t": msg_type,
        "ts": int(config.get_unix_time()),
        "rt": {
            "route_id": "to_node",
            "hops": routing_path
        },
        "pld": data_payload
    }

    # Use the resolved next-hop MAC in the routing path, falling back to broadcast
    phys_mac = (routing_path[0] if routing_path else target_mac_str) or "ff:ff:ff:ff:ff:ff"
    next_hop_bytes = mac_to_bytes(phys_mac)

    try:
        add_peer_safe(_e, next_hop_bytes)
        payload_str = ujson.dumps(envelope)
        try:
            _e.send(next_hop_bytes, payload_str.encode('utf-8'))
            if phys_mac == "ff:ff:ff:ff:ff:ff":
                print(" ESP-NOW broadcasted message")
            else:
                print(f" ESP-NOW unicasted message to {phys_mac}")
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
    try:
        import led_status
        led_status.set_status("START_DISCOVERY")
    except Exception as e:
        print(" Failed to set LED status to START_DISCOVERY:", e)

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

    # Keep Wi-Fi in STA mode for WAN connectivity and avoid reconfiguring
    # the radio path while ESP-NOW traffic is being processed.
    sta = network.WLAN(network.STA_IF)
    try:
        sta.active(True)
        try:
            sta.config(pm=network.WLAN.PM_NONE)
        except:
            pass
    except:
        pass

    try:
        ap = network.WLAN(network.AP_IF)
        ap.active(False)
    except:
        pass

    # In ESP-NOW-only mode, no WAN thread sets the radio channel for us.
    try:
        cfg = config.load_config()
        if cfg.get("client", {}).get("espnow_only", False):
            active_ch = cfg.get("wifi", {}).get("channel", 6)
            sta.config(channel=active_ch)
            print(f" ESP-NOW-only test channel: {active_ch}")
    except Exception as ch_err:
        print(f" ESP-NOW-only channel notice: {ch_err}")

    _e = espnow.ESPNow()
    _e.active(True)
    try:
        _e.config(rxbuf=4096)
    except Exception as ex:
        print("rxbuf config notice:", ex)

    _last_beacon_time = 0
    was_discovery_active = False

    while True:
        if heartbeats is not None:
            heartbeats["esp_now"] = time.time()

        # Handle mesh discovery beaconing
        now = config.get_unix_time()
        if now < _discovery_active_until:
            if not was_discovery_active:
                was_discovery_active = True
                try:
                    import led_status
                    led_status.set_status("START_DISCOVERY")
                except:
                    pass

            if now - _last_beacon_time >= 1.5:
                _last_beacon_time = now
                try:
                    active_ch = sta.config('channel')
                except Exception:
                    active_ch = 4
                hub_mac_str = bytes_to_mac(sta.config('mac'))
                beacon_pkt = {
                    "src": "hub_master_01",
                    "dst": "broadcast",
                    "t": "BEACON",
                    "ts": int(config.get_unix_time()),
                    "rt": {"hops": ["ff:ff:ff:ff:ff:ff"]},
                    "pld": {
                        "hub_mac": hub_mac_str,
                        "sender_mac": hub_mac_str,
                        "channel": active_ch
                    }
                }
                try:
                    add_peer_safe(_e, b'\xff\xff\xff\xff\xff\xff')
                    _e.send(b'\xff\xff\xff\xff\xff\xff', ujson.dumps(beacon_pkt).encode('utf-8'))
                except Exception as b_err:
                    print(" Beacon send notice:", b_err)
        else:
            if was_discovery_active:
                was_discovery_active = False
                try:
                    import led_status
                    led_status.set_status("MQTT_CONNECTED")
                except:
                    pass

        try:
            host, msg = _e.recv(5000)
            print(f" ESP-NOW recv: host={bytes_to_mac(host) if host else None}, msg_len={len(msg) if msg else 0}")
            print(f"  DEBUG: Raw msg from _e.recv(): {msg!r}") # Added debug print to see the actual bytes
            print(f"  Current recv_buffers keys: {list(recv_buffers.keys())}")
            
            if not host or not msg:
                time.sleep_ms(5) # Yield to other threads like MQTT
                continue

            sender_mac_str = bytes_to_mac(host)
            # Proactively add any sender as a peer to ensure reliable unicast reception.
            add_peer_safe(_e, host)
            
            gc.collect() # Ensure memory is clean before processing

            # ESP-NOW delivers this application packet as one datagram. Do not
            # append later datagrams to an old JSON buffer; a stray fragment such
            # as b'{' would otherwise poison the next packet.
            try:
                payload_str = bytes(msg).decode('utf-8')
                packet = parse_packet(payload_str)
            except Exception:
                print(f"  Ignoring invalid ESP-NOW datagram from {sender_mac_str}")
                recv_buffers[sender_mac_str] = b''
                recv_last_seen[sender_mac_str] = now
                continue

            recv_buffers[sender_mac_str] = b''
            recv_last_seen[sender_mac_str] = now
            print(f" ESP-NOW packet received from {sender_mac_str}: {packet.get('msg_type')}")
            print(f"  Payload: {packet.get('data')}")

            msg_type = packet.get("msg_type")
            target = packet.get("target", "")
            source = packet.get("source")
            data = packet.get("data", {})

            if packet:
                cfg = config.load_config()
                client_id = cfg.get("client", {}).get("id", "hub_master_01")
                hub_sta_mac = bytes_to_mac(sta.config('mac'))

                try:
                    ap = network.WLAN(network.AP_IF)
                    hub_ap_mac = bytes_to_mac(ap.config('mac'))
                except Exception:
                    hub_ap_mac = hub_sta_mac

                is_broadcast = target in ("00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff", "FF:FF:FF:FF:FF:FF", "broadcast")
                is_for_us = is_broadcast or (target and target.lower() in (client_id.lower(), "hub_master_01")) or (target and target.upper() in (hub_sta_mac.upper(), hub_ap_mac.upper()))

                if not is_for_us:
                    continue

                if msg_type == "PAIR_REQ" or (msg_type == "STATUS" and data.get("status") == "pairing_request"):
                    node_type = data.get("node_type", "UNKNOWN")
                    node_id = source or data.get("node_id") or data.get("custom_name", "")
                    custom_name = data.get("custom_name")
                    print(f"PAIR_REQ from {sender_mac_str} ({node_type}/{node_id})")

                    node_info = save_node(sender_mac_str, node_type, node_id=node_id, name=custom_name)

                    active_ch = 6
                    try:
                        active_ch = sta.config('channel')
                    except Exception:
                        pass

                    send_espnow_msg(sender_mac_str, {
                        "msg_type": "ACK",
                        "payload": {"status": "paired", "hub_mac": hub_sta_mac, "channel": active_ch}
                    }, target_id=node_id)

                    type_slug = node_type.lower()
                    device_id = node_info.get("node_id", "").lower() or sender_mac_str.replace(':', '')

                    site = cfg.get("client", {}).get("site", "default_site")
                    group = cfg.get("client", {}).get("group", "all")

                    if site == "default_site":
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
                    mqtt_client.publish_msg(f"{site}/{group}/{type_slug}/{device_id}/status", status_payload, retain=True)

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
                    site = cfg.get("client", {}).get("site", "default_site")
                    group = cfg.get("client", {}).get("group", "all")
                    if site == "default_site":
                        continue

                    nodes = load_nodes()
                    node_info = nodes.get(sender_mac_str, {})
                    node_type = node_info.get("node_type", "node").lower()
                    device_id = node_info.get("node_id", sender_mac_str.replace(':', '')).lower()

                    tele_topic = f"{site}/{group}/{node_type}/{device_id}/telemetry"
                    tele_payload = data.copy() if isinstance(data, dict) else {"data": data}
                    tele_payload["device_id"] = device_id
                    tele_payload["node_mac"] = sender_mac_str

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
                    site = cfg.get("client", {}).get("site", "default_site")
                    group = cfg.get("client", {}).get("group", "all")
                    if site == "default_site":
                        continue

                    nodes = load_nodes()
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
                    mqtt_client.publish_msg(f"{site}/{group}/{node_type}/{device_id}/command/response", mqtt_payload)
                    mqtt_client.publish_msg(f"{site}/{group}/{node_type}/{device_id}/acks", mqtt_payload)
                    mqtt_client.publish_msg(f"farm/{client_id}/command_response", mqtt_payload)

                elif msg_type in ("ALERT", "ALERTS"):
                    site = cfg.get("client", {}).get("site", "default_site")
                    group = cfg.get("client", {}).get("group", "all")
                    if site == "default_site":
                        continue

                    nodes = load_nodes()
                    node_info = nodes.get(sender_mac_str, {})
                    node_type = node_info.get("node_type", "node").lower()
                    device_id = node_info.get("node_id", sender_mac_str.replace(':', '')).lower()

                    alert_topic = f"{site}/{group}/{node_type}/{device_id}/alerts"
                    alert_payload = data.copy() if isinstance(data, dict) else {"data": data}
                    alert_payload["device_id"] = device_id
                    alert_payload["device_type"] = node_type.upper()
                    alert_payload["node_mac"] = sender_mac_str

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

            recv_buffers[sender_mac_str] = b''
            recv_last_seen[sender_mac_str] = now

        except Exception as e:
            err_str = str(e)
            if "buffer error" not in err_str:
                print(f"Error in espnow_receiver_thread loop: {e}")
            time.sleep_ms(100)
