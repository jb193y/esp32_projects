# esp_now_master.py (Hub)
import network
import espnow
import ujson
import time
import random
import ubinascii
import os
import gc
import config
import mqtt_client
import scheduler
import message_builder
import espnow_ota

_e = None
NODES_FILE = "nodes.json"
# Per-sender receive buffers to assemble fragmented ESP-NOW packets
recv_buffers = {}
recv_last_seen = {}
MAX_RX_BUFFER = 2048

registered_peers = []
tx_queue = config.Queue()
rx_queue = config.Queue()

# Node Command Mailbox for sleep/polling nodes (target_mac -> list of {msg_dict, routing_path, target_id, expires_at})
_node_mailbox = {}
MAILBOX_TTL_SEC = 600

def enqueue_mailbox_command(target_mac_str, msg_dict, routing_path=None, target_id=None, ttl_sec=MAILBOX_TTL_SEC):
    global _node_mailbox
    now = time.time()
    mac_key = target_mac_str.lower()
    if mac_key not in _node_mailbox:
        _node_mailbox[mac_key] = []
    
    # Remove expired commands
    _node_mailbox[mac_key] = [item for item in _node_mailbox[mac_key] if item["expires_at"] > now]
    
    _node_mailbox[mac_key].append({
        "msg_dict": msg_dict,
        "routing_path": routing_path,
        "target_id": target_id,
        "expires_at": now + ttl_sec
    })
    print(f" [Mailbox] Enqueued command for {target_mac_str} (queue length: {len(_node_mailbox[mac_key])})")

def pop_mailbox_command(target_mac_str):
    global _node_mailbox
    now = time.time()
    mac_key = target_mac_str.lower()
    if mac_key not in _node_mailbox:
        return None
    
    # Filter out expired items
    valid_items = [item for item in _node_mailbox[mac_key] if item["expires_at"] > now]
    if not valid_items:
        _node_mailbox.pop(mac_key, None)
        return None
    
    item = valid_items.pop(0)
    _node_mailbox[mac_key] = valid_items
    if not valid_items:
        _node_mailbox.pop(mac_key, None)
    return item

def reregister_peers(e):
    print(f" Re-registering {len(registered_peers)} peers...")
    for mac_str in registered_peers:
        try:
            e.add_peer(mac_to_bytes(mac_str))
        except OSError:
            pass

def extract_complete_frame(buf):
    """Return (frame_bytes, remainder) if a full frame is available, otherwise (None, buf)."""
    if len(buf) < 2:
        return None, buf
    frame_len = int.from_bytes(buf[:2], 'big')
    total_len = 2 + frame_len
    if len(buf) < total_len:
        return None, buf
    return buf[2:total_len], buf[total_len:]


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

def save_node(mac_str, node_type, node_id=None, name=None, parent=None):
    nodes = load_nodes()
    nodes[mac_str] = {
        "node_type": node_type,
        "node_id": node_id or mac_str.replace(':', '')[-8:],
        "custom_name": name or "{}_{}".format(node_type, mac_str[-5:].replace(':', '')),
        "paired_at": config.get_unix_time()
    }
    if parent and parent != mac_str:
        nodes[mac_str]["parent"] = parent
    elif "parent" in nodes[mac_str]:
        nodes[mac_str].pop("parent")

    with open(NODES_FILE, 'w') as f:
        ujson.dump(nodes, f)
    print(f" Node saved to registry: {mac_str} -> {node_type} (parent: {parent})")
    return nodes[mac_str]

def add_peer_safe(e, peer_bytes, channel=0):
    """Add/update an ESP-NOW peer. add_peer is idempotent."""
    peer_bytes = bytes(peer_bytes)
    mac_str = bytes_to_mac(peer_bytes)
    if mac_str not in registered_peers:
        registered_peers.append(mac_str)
    try:
        e.add_peer(peer_bytes, b'', channel, network.STA_IF)
    except OSError as ose:
        # Ignore 'ESP-NOW peer already exists' error, which is expected.
        err = ose.args[0] if ose.args else None
        if err not in (23, 12293, 12395, -12395) and 'ESP_ERR_ESPNOW_EXIST' not in str(ose):
            print(f"add_peer_safe notice: {ose}")

def parse_packet(payload_str):
    p = ujson.loads(payload_str)
    msg_type = p.get("msg_type") or p.get("t")
    target = p.get("target") or p.get("dst", "")
    source = p.get("source") or p.get("src")
    
    route = p.get("route") or p.get("rt", {})
    hops = (route.get("hops") or route.get("h")) if isinstance(route, dict) else []
    if not hops:
        hops = p.get("routing_path") or p.get("path") or []
        
    current_hop_index = (route.get("current_hop_index") or route.get("chi")) if isinstance(route, dict) else 0
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
    broadcast_only = cfg.get("client", {}).get("espnow_broadcast_only", False)

    if not routing_path:
        routing_path = [target_mac_str]

    msg_type = msg_dict.get("msg_type", "COMMAND")
    if msg_type == "CMD":
        msg_type = "COMMAND"
    data_payload = msg_dict.get("payload", {})

    envelope = message_builder.build_espnow_envelope(
        hub_id,
        target_id or target_mac_str,
        msg_type,
        data_payload,
        route_id="to_node",
        hops=routing_path
    )

    # Use the resolved next-hop MAC in the routing path, falling back to broadcast
    phys_mac = "ff:ff:ff:ff:ff:ff" if broadcast_only else ((routing_path[0] if routing_path else target_mac_str) or "ff:ff:ff:ff:ff:ff")
    next_hop_bytes = mac_to_bytes(phys_mac)

    try:
        payload_str = config.compact_json(envelope)
        frame_bytes = config.make_frame(payload_str)
        tx_queue.put((next_hop_bytes, frame_bytes, phys_mac, target_id or target_mac_str))
        return True
    except Exception as e:
        print(" Failed to enqueue ESP-NOW packet:", e)
        return False

# Initialize OTASender instance for ESP-NOW firmware broadcasting
ota_sender = espnow_ota.OTASender(send_espnow_msg)

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
        site = cfg.get("client", {}).get("site", "default_site")
        group = cfg.get("client", {}).get("group", "all")
        if site != "default_site":
            mqtt_client.publish_msg(f"{site}/{group}/all/acks", fwd_payload)

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
    site = cfg.get("client", {}).get("site", "default_site")
    group = cfg.get("client", {}).get("group", "all")
    if site != "default_site":
        mqtt_client.publish_msg(f"{site}/{group}/{node_type}/{node_id_slug}/acks", fwd_payload)

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
        parent_mac = node_info.get("parent")
        if parent_mac and parent_mac != target_mac_str:
            mac_routing_path = [parent_mac, target_mac_str]
        else:
            mac_routing_path = [target_mac_str]

    if node_info.get("node_type") == "PUMP" and command == "PUMP_ON":
        deficit = args.get("deficit", 10.0)
        duration = args.get("duration", 300)
        scheduler.queue_irrigation(target_mac_str, deficit, duration)
    elif command == "OTA":
        def _bg_ota_task(mac, node_id, args_dict, n_type, s_site, s_group):
            try:
                base_url = args_dict.get("url") or cfg.get("ota", {}).get("base_url", "http://10.10.10.211:8000/fw")
                manifest_name = args_dict.get("manifest_name") or cfg.get("ota", {}).get("manifest", "manifest.json")
                
                print(f" [Hub OTA Task] Starting ESP-NOW OTA for node {node_id} from {base_url}")
                if s_site != "default_site":
                    mqtt_client.publish_msg(f"{s_site}/{s_group}/{n_type}/{node_id}/acks", {
                        "source": client_id, "target": "backend_api", "msg_type": "ACK",
                        "timestamp": config.get_unix_time(),
                        "data": {"status": "OTA_STAGING_ON_HUB", "target_node": node_id, "url": base_url}
                    })
                
                version, cached_files = ota_sender.fetch_manifest_and_stage(base_url, manifest_name)
                ota_sender.stream_firmware_to_node(mac, node_id, version, cached_files)
                
                if s_site != "default_site":
                    mqtt_client.publish_msg(f"{s_site}/{s_group}/{n_type}/{node_id}/acks", {
                        "source": client_id, "target": "backend_api", "msg_type": "ACK",
                        "timestamp": config.get_unix_time(),
                        "data": {"status": "OTA_COMPLETED", "target_node": node_id, "version": version}
                    })
            except Exception as ota_err:
                print(f" [Hub OTA Task] Error updating node {node_id}:", ota_err)
                if s_site != "default_site":
                    mqtt_client.publish_msg(f"{s_site}/{s_group}/{n_type}/{node_id}/acks", {
                        "source": client_id, "target": "backend_api", "msg_type": "ACK",
                        "timestamp": config.get_unix_time(),
                        "data": {"status": "OTA_FAILED", "target_node": node_id, "error": str(ota_err)}
                    })
                
        import _thread
        _thread.start_new_thread(_bg_ota_task, (target_mac_str, target_node, args, node_type, site, group))
    else:
        payload = {"cmd": command}
        payload.update(args)
        cmd_msg = {
            "msg_type": "COMMAND",
            "payload": payload
        }
        # 1. Enqueue in mailbox for sleep/polling nodes
        enqueue_mailbox_command(target_mac_str, cmd_msg, mac_routing_path, target_id=target_node)
        # 2. Also send directly in case the node is currently awake
        send_espnow_msg(target_mac_str, cmd_msg, mac_routing_path, target_id=target_node)

def hub_tx_loop():
    global _e
    print(" ESP-NOW Master TX Loop Thread Started")
    while True:
        try:
            item = tx_queue.get()
            if item is None:
                time.sleep_ms(50)  # Yield CPU
                continue
                
            next_hop_bytes, payload_bytes, phys_mac, target_id = item
            
            if _e is None:
                time.sleep_ms(100)
                tx_queue.put(item)
                continue
                
            try:
                if phys_mac != "ff:ff:ff:ff:ff:ff":
                    add_peer_safe(_e, next_hop_bytes)
                res = config.send_fragmented(_e, next_hop_bytes, payload_bytes)
                if phys_mac == "ff:ff:ff:ff:ff:ff":
                    print(" ESP-NOW broadcasted message")
                else:
                    print(f" ESP-NOW unicasted message to {phys_mac}")
            except Exception as send_err:
                print(" [Hub TX Queue] ESP-NOW send error:", send_err)
                if "buffer error" in str(send_err):
                    try:
                        _e.active(False)
                    except:
                        pass
                    time.sleep_ms(50)
                    try:
                        _e = espnow.ESPNow()
                        _e.active(True)
                        _e.config(rxbuf=4096)
                        reregister_peers(_e)
                    except:
                        pass
            
            time.sleep_ms(50)
            
        except Exception as loop_err:
            print(" [Hub TX Queue] Error in tx loop:", loop_err)
            time.sleep_ms(100)

def process_espnow_frame(sender_mac_str, payload_bytes):
    try:
        payload_str = payload_bytes.decode('utf-8')
    except Exception as decode_err:
        print(f"  Ignoring non-UTF-8 payload from {sender_mac_str}: {decode_err} | Raw: {payload_bytes}")
        return
        
    try:
        packet = parse_packet(payload_str)
    except Exception as parse_err:
        print(f"  Ignoring invalid ESP-NOW packet frame from {sender_mac_str}: {parse_err}")
        return

    sta = network.WLAN(network.STA_IF)
    hub_sta_mac = bytes_to_mac(sta.config('mac'))
    try:
        ap = network.WLAN(network.AP_IF)
        hub_ap_mac = bytes_to_mac(ap.config('mac'))
    except Exception:
        hub_ap_mac = hub_sta_mac

        msg_type = packet.get("msg_type")
        target = packet.get("target", "")
        source = packet.get("source")
        data = packet.get("data", {})

        cfg = config.load_config()
        client_id = cfg.get("client", {}).get("id", "hub_master_01")

        is_broadcast = target in ("00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff", "FF:FF:FF:FF:FF:FF", "broadcast")
        is_for_us = is_broadcast or (target and ("hub" in target.lower() or target.lower() in (client_id.lower(), "hub_master_01", "hub_master_02"))) or (target and target.upper() in (hub_sta_mac.upper(), hub_ap_mac.upper()))

        if not is_for_us:
            return

        if isinstance(data, dict):
            if data.get("resource_surplus") or data.get("solar_surplus"):
                try:
                    import scheduler
                    res_name = data.get("resource") or "solar"
                    duration = int(data.get("duration_sec") or 1800)
                    scheduler.set_resource_surplus(res_name, duration)
                except Exception as ex:
                    print("Error setting inline ESP-NOW resource surplus:", ex)

        if msg_type == "DISCOVERY_REQ":
            print(f"DISCOVERY_REQ from {sender_mac_str} ({source})")
            active_ch = 6
            try:
                active_ch = sta.config('channel')
            except Exception:
                pass
            
            resp_payload = {
                "status": "discovery_response",
                "hub_mac": hub_sta_mac,
                "parent_mac": hub_sta_mac,
                "channel": active_ch,
                "hop_count": 0,
                "hub_freshness": 0
            }
            
            send_espnow_msg(sender_mac_str, {
                "msg_type": "DISCOVERY_RESP",
                "payload": resp_payload
            }, target_id=source)
            return

        if msg_type == "PAIR_REQ" or (msg_type == "STATUS" and data.get("status") == "pairing_request"):
            node_type = data.get("node_type", "UNKNOWN")
            node_id = source or data.get("node_id") or data.get("custom_name", "")
            custom_name = data.get("custom_name")
            
            # Support relayed pairing requests
            actual_mac = data.get("mac") or sender_mac_str
            print(f"PAIR_REQ from {actual_mac} ({node_type}/{node_id}) via {sender_mac_str}")

            node_info = save_node(actual_mac, node_type, node_id=node_id, name=custom_name, parent=sender_mac_str)

            active_ch = 6
            try:
                active_ch = sta.config('channel')
            except Exception:
                pass

            # Build reverse routing path for multi-hop ACK delivery
            reverse_path = [sender_mac_str]
            if actual_mac != sender_mac_str:
                reverse_path.append(actual_mac)

            send_espnow_msg(sender_mac_str, {
                "msg_type": "ACK",
                "payload": {"status": "paired", "hub_mac": hub_sta_mac, "channel": active_ch}
            }, routing_path=reverse_path, target_id=node_id)

            type_slug = node_type.lower()
            device_id = node_info.get("node_id", "").lower() or actual_mac.replace(':', '')

            site = cfg.get("client", {}).get("site", "default_site")
            group = cfg.get("client", {}).get("group", "all")

            if site == "default_site":
                return

            status_payload = message_builder.build_mqtt_payload(
                source=device_id,
                target="backend_api",
                msg_type="STATUS",
                data={
                    "device_id": device_id,
                    "status": "online",
                    "node_type": node_type,
                    "mac": actual_mac
                },
                route_transport="ESPNOW",
                route_id=packet.get("route", {}).get("route_id") or packet.get("route", {}).get("rid", "direct"),
                current_hop_index=packet.get("current_hop_index", 0),
                hops=packet.get("hops", [])
            )
            mqtt_client.publish_msg(f"{site}/{group}/{type_slug}/{device_id}/status", status_payload, retain=True)

            new_node_payload = message_builder.build_mqtt_payload(
                source="hub_master_01",
                target="backend_api",
                msg_type="STATUS",
                data={
                    "mac": actual_mac,
                    "device_type": node_type,
                    "device_id": device_id,
                    "custom_name": node_info["custom_name"]
                },
                route_transport="MQTT",
                route_id="hub_discovery_notice",
                current_hop_index=0,
                hops=["backend_api"]
            )
            mqtt_client.publish_msg("farm/config/new_node_added", new_node_payload)

        elif msg_type in ("TELE", "TELEMETRY"):
            site = cfg.get("client", {}).get("site", "default_site")
            group = cfg.get("client", {}).get("group", "all")
            if site == "default_site":
                return

            nodes = load_nodes()
            
            # Resolve original sender MAC address from packet source ID
            original_sender_mac = None
            for mac, info in nodes.items():
                if info.get("node_id") == source or info.get("custom_name") == source:
                    original_sender_mac = mac
                    break
            if not original_sender_mac:
                original_sender_mac = sender_mac_str

            node_info = nodes.get(original_sender_mac, {})
            node_type = node_info.get("node_type", "node").lower()
            device_id = node_info.get("node_id", original_sender_mac.replace(':', '')).lower()

            tele_topic = f"{site}/{group}/{node_type}/{device_id}/telemetry"
            tele_payload = data.copy() if isinstance(data, dict) else {"data": data}
            tele_payload["device_id"] = device_id
            tele_payload["node_mac"] = original_sender_mac

            mqtt_payload = message_builder.build_mqtt_payload(
                source=source or device_id,
                target="hub_master_01",
                msg_type="TELEMETRY",
                data=tele_payload,
                route_transport="ESPNOW",
                route_id=packet.get("route", {}).get("route_id") or packet.get("route", {}).get("rid", "direct"),
                current_hop_index=packet.get("current_hop_index", 0),
                hops=packet.get("hops", []),
                timestamp=packet.get("raw", {}).get("timestamp", int(config.get_unix_time()))
            )
            mqtt_client.publish_msg(tele_topic, mqtt_payload)

            # Compute the reverse routing path for multi-hop ACK delivery
            incoming_hops = packet.get("hops", [])
            relay_hops = []
            if incoming_hops:
                relay_hops = list(incoming_hops)
                if len(relay_hops) > 0 and (relay_hops[-1].lower() == hub_sta_mac.lower() or relay_hops[-1].lower() == hub_ap_mac.lower()):
                    relay_hops.pop()
            
            return_hops = []
            for hop in reversed(relay_hops):
                return_hops.append(hop)
            return_hops.append(original_sender_mac)

            # Check if this node has a pending command in the mailbox to deliver
            pending_cmd = pop_mailbox_command(original_sender_mac) or pop_mailbox_command(sender_mac_str)
            try:
                if pending_cmd:
                    print(f" [Hub Mailbox] Delivering pending command to waking node {original_sender_mac}")
                    send_espnow_msg(
                        target_mac_str=return_hops[0],
                        msg_dict=pending_cmd["msg_dict"],
                        routing_path=return_hops,
                        target_id=device_id
                    )
                else:
                    # Send ESP-NOW ACK with SLEEP_OK so deep-sleeping nodes can immediately sleep
                    send_espnow_msg(
                        target_mac_str=return_hops[0],
                        msg_dict={
                            "msg_type": "ACK",
                            "payload": {
                                "status": "sleep_ok",
                                "sleep_sec": 30,
                                "topic": tele_topic
                            }
                        },
                        routing_path=return_hops,
                        target_id=device_id
                    )
            except Exception as ack_err:
                print(" [Hub] Response delivery error:", ack_err)

        elif msg_type == "ACK":
            # Forward to ota_sender if this is an OTA protocol packet
            if isinstance(data, dict) and ("ota_proto" in data or str(data.get("status", "")).startswith("OTA_") or str(data.get("status", "")).startswith("CHUNK_") or str(data.get("status", "")).startswith("VERIFY_")):
                ota_sender.notify_ack(data)

            site = cfg.get("client", {}).get("site", "default_site")
            group = cfg.get("client", {}).get("group", "all")
            if site == "default_site":
                return

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

            mqtt_payload = message_builder.build_mqtt_payload(
                source=source or device_id,
                target="hub_master_01",
                msg_type="ACK",
                data=exec_payload,
                route_transport="ESPNOW",
                route_id=packet.get("route", {}).get("route_id") or packet.get("route", {}).get("rid", "direct"),
                current_hop_index=packet.get("current_hop_index", 0),
                hops=packet.get("hops", []),
                timestamp=packet.get("raw", {}).get("timestamp", int(config.get_unix_time()))
            )
            mqtt_client.publish_msg(f"{site}/{group}/{node_type}/{device_id}/acks", mqtt_payload)

        elif msg_type in ("ALERT", "ALERTS"):
            site = cfg.get("client", {}).get("site", "default_site")
            group = cfg.get("client", {}).get("group", "all")
            if site == "default_site":
                return

            nodes = load_nodes()
            node_info = nodes.get(sender_mac_str, {})
            node_type = node_info.get("node_type", "node").lower()
            device_id = node_info.get("node_id", sender_mac_str.replace(':', '')).lower()

            alert_topic = f"{site}/{group}/{node_type}/{device_id}/alerts"
            alert_payload = data.copy() if isinstance(data, dict) else {"data": data}
            alert_payload["device_id"] = device_id
            alert_payload["device_type"] = node_type.upper()
            alert_payload["node_mac"] = sender_mac_str

            mqtt_payload = message_builder.build_mqtt_payload(
                source=source or device_id,
                target="hub_master_01",
                msg_type="ALERT",
                data=alert_payload,
                route_transport="ESPNOW",
                route_id=packet.get("route", {}).get("route_id") or packet.get("route", {}).get("rid", "direct"),
                current_hop_index=packet.get("current_hop_index", 0),
                hops=packet.get("hops", []),
                timestamp=packet.get("raw", {}).get("timestamp", int(config.get_unix_time()))
            )
            mqtt_client.publish_msg(alert_topic, mqtt_payload)
        elif msg_type == "RESOURCE_SURPLUS":
            try:
                import scheduler
                res_name = data.get("resource") or data.get("resource_name") or "solar"
                duration = int(data.get("duration_sec") or data.get("duration") or 1800)
                scheduler.set_resource_surplus(res_name, duration)
            except Exception as ex:
                print("Error setting ESP-NOW resource surplus command:", ex)

        # Explicitly yield and collect garbage after processing each packet
        gc.collect()

    except Exception as loop_err:
        import sys
        print(" [Hub RX Processor] Error processing frame:")
        sys.print_exception(loop_err)

def espnow_test_receiver_thread(heartbeats=None):
    """Minimal raw ESP-NOW receiver for transport-only testing."""
    global _e
    print(" ESP-NOW Test Receiver Started")

    sta = network.WLAN(network.STA_IF)
    sta.active(True)
    try:
        sta.config(pm=network.WLAN.PM_NONE)
    except Exception:
        pass

    try:
        ap = network.WLAN(network.AP_IF)
        ap.active(False)
    except Exception:
        pass

    try:
        cfg = config.load_config()
        channel = cfg.get("wifi", {}).get("channel", 6)
        try:
            sta.config(channel=channel)
        except Exception:
            try:
                sta.disconnect()
            except Exception:
                pass
            sta.config(channel=channel)
        print(f" ESP-NOW Test channel: {channel}")
    except Exception as channel_err:
        print(f" ESP-NOW Test channel notice: {channel_err}")

    def init_test_espnow():
        global _e
        _e = espnow.ESPNow()
        _e.active(True)
        try:
            cfg = config.load_config()
            test_peer = cfg.get("client", {}).get(
                "espnow_test_peer", "dc:b4:d9:14:2d:50"
            )
            # Match the known-good standalone ESP-NOW test API exactly.
            _e.add_peer(mac_to_bytes(test_peer))
            print(f" ESP-NOW Test peer: {test_peer}")
        except Exception as peer_err:
            print(f" ESP-NOW Test peer notice: {peer_err}")
        reregister_peers(_e)

    init_test_espnow()

    # Spawn Hub TX Loop Thread for Test Mode
    import _thread
    _thread.start_new_thread(hub_tx_loop, ())

    print(" ESP-NOW Test Receiver Listening")
    test_peers = {}
    command_sequence = 0
    test_cfg = config.load_config().get("client", {})
    command_min_sec = test_cfg.get("espnow_command_min_sec", 5)
    command_max_sec = test_cfg.get("espnow_command_max_sec", 12)
    command_choices = tuple(test_cfg.get(
        "espnow_test_commands", ["GET_STATUS", "COM_TEST"]
    ))
    next_command_at = time.time() + random.randint(command_min_sec, command_max_sec)
    while True:
        if heartbeats is not None:
            heartbeats["esp_now"] = time.time()

        try:
            host, msg = _e.recv(1000)
            if not host or not msg:
                if test_peers and time.time() >= next_command_at:
                    peer_mac = random.choice(list(test_peers.keys()))
                    peer_info = test_peers[peer_mac]
                    command_sequence += 1
                    command = random.choice(command_choices)
                    command_packet = {
                        "src": "hub_test",
                        "dst": peer_info["id"],
                        "t": "COMMAND",
                        "ts": int(config.get_unix_time()),
                        "rt": {"hops": [peer_mac]},
                        "pld": {
                            "cmd": command,
                            "test_seq": command_sequence
                        }
                    }
                    command_bytes = mac_to_bytes(peer_mac)
                    tx_queue.put((command_bytes, config.make_frame(config.compact_json(command_packet)), peer_mac, peer_info["id"]))
                    print(f" [Test] Enqueued CMD to {peer_mac} ({peer_info['id']}): {command}")
                    next_command_at = time.time() + random.randint(command_min_sec, command_max_sec)
                time.sleep_ms(10)
                continue

            sender = bytes_to_mac(host)
            
            # Clean up stale buffer
            now = time.time()
            if now - recv_last_seen.get(sender, now) > 10:
                recv_buffers[sender] = b""
            recv_last_seen[sender] = now

            if sender not in recv_buffers:
                recv_buffers[sender] = b""
            recv_buffers[sender] += bytes(msg)

            # VC1 and VC2 may both talk to this test hub. Register each sender
            try:
                _e.add_peer(mac_to_bytes(sender))
            except OSError:
                pass

            while True:
                payload_bytes, remainder = extract_complete_frame(recv_buffers[sender])
                if payload_bytes is None:
                    if len(recv_buffers[sender]) > MAX_RX_BUFFER:
                        recv_buffers[sender] = b""
                    break

                recv_buffers[sender] = remainder
                try:
                    payload_str = payload_bytes.decode('utf-8')
                except Exception as decode_err:
                    print(f"  [Test] Ignoring non-UTF-8 payload from {sender}: {decode_err}")
                    continue
                print(f" ESP-NOW TEST RX from {sender}, payload={payload_str}")

                try:
                    incoming = ujson.loads(payload_str)
                    incoming_type = incoming.get("t", "UNKNOWN")
                    incoming_data = incoming.get("pld", {})
                    logical_sender = incoming.get("src") or sender
                    
                    test_peers[sender] = {"id": logical_sender}
                    cfg = config.load_config()
                    channel = cfg.get("wifi", {}).get("channel", 6)

                    if incoming_type == "ACK":
                        print(f" ESP-NOW TEST ACK from {sender}: {incoming_data}")
                        continue

                    if incoming_type == "STATUS" and isinstance(incoming_data, dict) and incoming_data.get("status") == "pairing_request":
                        reply_data = {
                            "status": "paired",
                            "hub_mac": bytes_to_mac(sta.config('mac')),
                            "channel": channel
                        }
                    else:
                        reply_data = {
                            "status": "received",
                            "received_type": incoming_type,
                            "received_from": sender
                        }

                    reply = {
                        "src": "hub_test",
                        "dst": logical_sender,
                        "t": "ACK",
                        "ts": int(config.get_unix_time()),
                        "rt": {"hops": [sender]},
                        "pld": reply_data
                    }

                    reply_peer = mac_to_bytes(sender)
                    tx_queue.put((reply_peer, config.make_frame(config.compact_json(reply)), sender, logical_sender))
                    print(f" [Test] Enqueued reply ACK to {sender}")

                except Exception as reply_err:
                    print(f" ESP-NOW Test reply error: {reply_err}")

        except Exception as recv_err:
            err_str = str(recv_err)
            if "buffer error" in err_str:
                try:
                    _e.active(False)
                except:
                    pass
                time.sleep_ms(50)
                init_test_espnow()
            else:
                print(f" ESP-NOW Test receive error: {recv_err}")
            time.sleep_ms(100)

def espnow_receiver_thread(heartbeats=None):
    global _e
    print(" ESP-NOW Master Receiver Thread Started")

    sta = network.WLAN(network.STA_IF)
    if not sta.active():
        try:
            sta.active(True)
        except Exception as sta_err:
            print("STA active notice:", sta_err)

    try:
        ap = network.WLAN(network.AP_IF)
        ap.active(False)
    except:
        pass

    try:
        cfg = config.load_config()
        if cfg.get("client", {}).get("espnow_only", False):
            active_ch = cfg.get("wifi", {}).get("channel", 6)
            try:
                sta.config(channel=active_ch)
            except Exception:
                try:
                    sta.disconnect()
                except Exception:
                    pass
                sta.config(channel=active_ch)
            print(f" ESP-NOW-only test channel: {active_ch}")
    except Exception as ch_err:
        print(f" ESP-NOW-only channel notice: {ch_err}")

    def init_real_espnow():
        global _e
        _e = espnow.ESPNow()
        _e.active(True)
        try:
            _e.config(rxbuf=4096)
        except Exception as ex:
            print("rxbuf config notice:", ex)
        reregister_peers(_e)

    init_real_espnow()
    
    # Spawn Hub TX loop
    import _thread
    _thread.start_new_thread(hub_tx_loop, ())
    
    print(" ESP-NOW Master Receiver active and listening")

    _last_beacon_time = 0
    was_discovery_active = False

    while True:
        if heartbeats is not None:
            heartbeats["esp_now"] = time.time()

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
                beacon_payload = {
                    "hub_mac": hub_mac_str,
                    "sender_mac": hub_mac_str,
                    "channel": active_ch
                }
                try:
                    add_peer_safe(_e, b'\xff\xff\xff\xff\xff\xff')
                    send_espnow_msg("ff:ff:ff:ff:ff:ff", {
                        "msg_type": "BEACON",
                        "payload": beacon_payload
                    })
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
            if not host or not msg:
                time.sleep_ms(5)
                continue

            sender_mac_str = bytes_to_mac(host)
            
            if now - recv_last_seen.get(sender_mac_str, now) > 10:
                recv_buffers[sender_mac_str] = b""
            recv_last_seen[sender_mac_str] = now

            if sender_mac_str not in recv_buffers:
                recv_buffers[sender_mac_str] = b""
            recv_buffers[sender_mac_str] += bytes(msg)

            cfg = config.load_config()
            if not cfg.get("client", {}).get("espnow_broadcast_only", False):
                add_peer_safe(_e, host)
            
            gc.collect()

            while True:
                payload_bytes, remainder = extract_complete_frame(recv_buffers[sender_mac_str])
                if payload_bytes is None:
                    if len(recv_buffers[sender_mac_str]) > MAX_RX_BUFFER:
                        recv_buffers[sender_mac_str] = b""
                    break

                recv_buffers[sender_mac_str] = remainder
                process_espnow_frame(sender_mac_str, payload_bytes)

        except Exception as e:
            err_str = str(e)
            if "buffer error" in err_str:
                try:
                    _e.active(False)
                except:
                    pass
                time.sleep_ms(50)
                try:
                    init_real_espnow()
                except:
                    pass
            else:
                print(f"Error in espnow_receiver_thread loop: {e}")
            time.sleep_ms(100)
