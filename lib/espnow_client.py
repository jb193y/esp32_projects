# espnow_client.py (Shared ESP-NOW Client Library)
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
import espnow_relay
import message_builder

def set_wifi_channel(ch):
    ap = network.WLAN(network.AP_IF)
    ap.active(True)
    try:
        ap.config(channel=ch)
    except Exception:
        pass
    sta = network.WLAN(network.STA_IF)
    sta.active(True)
    try:
        sta.config(channel=ch)
    except Exception:
        pass

_e = None
_paired = False
_last_hub_rx_time = time.time()
_last_pairing_tx_time = 0
_stop_requested = False
MAX_RX_BUFFER = 2048

tx_queue = config.Queue()

def client_tx_loop():
    global _e, _stop_requested
    print(" ESP-NOW Client TX Loop Thread Started")
    while not _stop_requested:
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
                add_peer_safe(_e, next_hop_bytes)
                res = config.send_fragmented(_e, next_hop_bytes, payload_bytes)
                print(f" [TX Queue] Envelope sent to next hop {phys_mac} for destination {target_id} (res={res})")
                try:
                    print(payload_bytes[2:].decode('utf-8'))
                except Exception:
                    pass
                print()
            except Exception as send_err:
                print(" [TX Queue] ESP-NOW send error:", send_err)
                if "buffer error" in str(send_err):
                    try:
                        _e.active(False)
                    except:
                        pass
                    time.sleep_ms(50)
                    try:
                        _e.active(True)
                    except:
                        pass
            
            import gc
            gc.collect()
            time.sleep_ms(50)
            
        except Exception as loop_err:
            print(" [TX Queue] Error in tx loop:", loop_err)
            time.sleep_ms(100)

def mac_to_bytes(mac_str):
    if not is_valid_mac(mac_str):
        return b'\xff\xff\xff\xff\xff\xff'
    try:
        return bytes(int(x, 16) for x in mac_str.split(':'))
    except Exception:
        return b'\xff\xff\xff\xff\xff\xff'

def bytes_to_mac(mac_bytes):
    return ':'.join('%02x' % b for b in mac_bytes)


def extract_complete_frame(buf):
    """Return (frame_bytes, remainder) if a full frame is available, otherwise (None, buf)."""
    if len(buf) < 2:
        return None, buf
    
    # Handle direct un-framed JSON payloads (e.g. b'{"pld"...')
    if buf[0] == 0x7b:
        return buf, b""

    frame_len = int.from_bytes(buf[:2], 'big')
    total_len = 2 + frame_len
    if len(buf) < total_len:
        return None, buf
    return buf[2:total_len], buf[total_len:]


def get_hub_id():
    cfg = config.load_config()
    hub_cfg = cfg.get("hub", {})
    if isinstance(hub_cfg, dict):
        for key in ("id", "node_id", "client_id"):
            hub_id = hub_cfg.get(key)
            if hub_id:
                return str(hub_id)

    return "hub_master_01"

def is_paired():
    global _paired
    if not _paired:
        try:
            cfg = config.load_config()
            hub_mac = cfg.get("hub", {}).get("mac", "")
            if is_valid_mac(hub_mac) and hub_mac != "00:00:00:00:00:00" and hub_mac != "ff:ff:ff:ff:ff:ff":
                _paired = True
        except Exception:
            pass
    return _paired

def set_paired(val):
    global _paired
    _paired = val

def stop_client():
    global _stop_requested, _paired, _e
    _stop_requested = True
    _paired = False
    if _e is not None:
        try:
            _e.active(False)
        except Exception:
            pass
    print(" ESP-NOW Client stopped")

def add_peer_safe(e, peer_bytes, channel=0):
    """Add/update an ESP-NOW peer. add_peer is idempotent and safe to call multiple times."""
    peer_bytes = bytes(peer_bytes)
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

def is_valid_mac(mac_str):
    if not isinstance(mac_str, str) or len(mac_str) != 17 or mac_str.count(':') != 5:
        return False
    try:
        for part in mac_str.split(':'):
            if len(part) != 2:
                return False
            int(part, 16)
        return True
    except Exception:
        return False

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
    if is_valid_mac(parent_mac) and parent_mac != "00:00:00:00:00:00" and parent_mac != "ff:ff:ff:ff:ff:ff":
        hops.append(parent_mac)
    if is_valid_mac(dest_mac) and dest_mac not in hops:
        hops.append(dest_mac)
        
    if not hops:
        hops = [dest_mac]
        
    return "df", hops

def send_ack_or_tele_to_hub(msg_type, payload, target_mac=None):
    global _e
    if _e is None:
        return False

    cfg = config.load_config()
    client_cfg = cfg.get("client", {})
    source_id = client_cfg.get("id", "unknown_node")
    broadcast_only = client_cfg.get("espnow_broadcast_only", False)

    # Only an explicit broadcast destination uses broadcast routing. A pairing
    # request can be a unicast STATUS packet when a hub MAC is configured.
    is_broadcast = (broadcast_only or target_mac == "ff:ff:ff:ff:ff:ff")
    target_id = "broadcast" if is_broadcast else get_hub_id()

    # Get pre-provisioned route or dynamic fallback
    route_id, hops = get_route_for_target(target_id, target_mac)
    if is_broadcast:
        hops = ["ff:ff:ff:ff:ff:ff"]

    envelope = message_builder.build_espnow_envelope(
        source_id,
        target_id,
        "STATUS" if msg_type == "PAIR_REQ" else msg_type,
        payload,
        route_id=route_id,
        hops=hops
    )

    # Use next-hop MAC from routing path if available, fallback to target_mac, then broadcast
    phys_mac = "ff:ff:ff:ff:ff:ff" if broadcast_only else ((hops[0] if hops else target_mac) or "ff:ff:ff:ff:ff:ff")
    next_hop_bytes = mac_to_bytes(phys_mac)

    try:
        payload_str = config.compact_json(envelope)
        frame_bytes = config.make_frame(payload_str)
        tx_queue.put((next_hop_bytes, frame_bytes, phys_mac, target_id))
        return True
    except Exception as err:
        print(f" Failed to enqueue packet to destination {target_id}:", err)
        return False

# Backward compatibility alias for pump controller
def send_to_hub(msg_type, payload):
    return send_ack_or_tele_to_hub(msg_type, payload)

_pair_channel_idx = 0

def send_direct_espnow(target_mac_str, target_id, msg_type, payload):
    global _e
    if _e is None:
        return False

    cfg = config.load_config()
    source_id = cfg.get("client", {}).get("id", "unknown_node")

    envelope = message_builder.build_espnow_envelope(
        source_id,
        target_id,
        msg_type,
        payload,
        route_id="direct",
        hops=[target_mac_str]
    )

    next_hop_bytes = mac_to_bytes(target_mac_str)
    try:
        payload_str = config.compact_json(envelope)
        frame_bytes = config.make_frame(payload_str)
        tx_queue.put((next_hop_bytes, frame_bytes, target_mac_str, target_id))
        return True
    except Exception as err:
        print(f" Failed to send direct packet to {target_id}:", err)
        return False

def send_discovery_request():
    global _e, _pair_channel_idx, _last_pairing_tx_time
    if _e is None:
        return False

    # Enforce a 2-second rate limit on discovery requests
    if time.time() - _last_pairing_tx_time < 2:
        return False
    _last_pairing_tx_time = time.time()

    cfg = config.load_config()
    client_cfg = cfg.get("client", {})
    source_id = client_cfg.get("id", "unknown_node")
    node_type = client_cfg.get("type", "client").upper()

    payload = {
        "status": "discovery_request",
        "node_type": node_type,
        "node_id": source_id,
        "custom_name": client_cfg.get("custom_name", "Client Node")
    }

    # Multi-Channel Mesh Scanning: cycle channels (4, 6, 1, 11) to discover nearby Hub/Repeater
    channels = [4, 6, 1, 11]
    ch = channels[_pair_channel_idx % len(channels)]
    _pair_channel_idx += 1
    set_wifi_channel(ch)
    print(f"Scanning Channel {ch}: Broadcasting DISCOVERY_REQ from {node_type}...")

    envelope = message_builder.build_espnow_envelope(
        source_id,
        "broadcast",
        "DISCOVERY_REQ",
        payload,
        route_id="discovery",
        hops=["ff:ff:ff:ff:ff:ff"]
    )

    try:
        payload_str = config.compact_json(envelope)
        frame_bytes = config.make_frame(payload_str)
        tx_queue.put((b'\xff\xff\xff\xff\xff\xff', frame_bytes, "ff:ff:ff:ff:ff:ff", "broadcast"))
        return True
    except Exception as err:
        print(" Failed to enqueue discovery packet:", err)
        return False

def send_pairing_request():
    global _e, _pair_channel_idx, _last_hub_rx_time, _last_pairing_tx_time
    if _e is None:
        return

    # Enforce a 2-second rate limit on pairing requests
    if time.time() - _last_pairing_tx_time < 2:
        return
    _last_pairing_tx_time = time.time()

    cfg = config.load_config()
    client_cfg = cfg.get("client", {})
    node_type = client_cfg.get("type", "client").upper()
    
    sta = network.WLAN(network.STA_IF)
    local_mac = bytes_to_mac(sta.config('mac'))
    
    payload = {
        "status": "pairing_request",
        "node_type": node_type,
        "node_id": client_cfg.get("id", ""),
        "custom_name": client_cfg.get("custom_name", "Client Node"),
        "mac": local_mac
    }

    hub_mac = cfg.get("hub", {}).get("mac", "")
    has_saved_hub = is_valid_mac(hub_mac) and hub_mac not in ("00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff")
    time_since_last_rx = time.time() - _last_hub_rx_time
    broadcast_only = client_cfg.get("espnow_broadcast_only", False)

    if broadcast_only:
        test_ch = cfg.get("wifi", {}).get("channel", 6)
        set_wifi_channel(test_ch)
        print(f"ESP-NOW broadcast test: staying on Channel {test_ch}")
        send_ack_or_tele_to_hub("STATUS", payload, target_mac="ff:ff:ff:ff:ff:ff")
        return

    # If we have a saved Hub, try contacting it on its saved channel for up to 20s
    # before starting dynamic multi-channel scanning
    if has_saved_hub and time_since_last_rx < 20:
        saved_ch = cfg.get("wifi", {}).get("channel", 4)
        set_wifi_channel(saved_ch)
        print(f"Sending PAIR_REQ unicast to Hub {hub_mac} on Channel {saved_ch}...")
        send_ack_or_tele_to_hub("STATUS", payload, target_mac=hub_mac)
    else:
        # Multi-Channel Mesh Scanning: cycle channels (4, 6, 1, 11) to discover nearby Hub/Repeater
        channels = [4, 6, 1, 11]
        ch = channels[_pair_channel_idx % len(channels)]
        _pair_channel_idx += 1
        set_wifi_channel(ch)
        print(f"Scanning Channel {ch}: Broadcasting PAIR_REQ from {node_type}...")
        send_ack_or_tele_to_hub("STATUS", payload, target_mac="ff:ff:ff:ff:ff:ff")


def is_paired():
    global _paired
    if _paired:
        return True
    cfg = config.load_config()
    hub_mac = cfg.get("hub", {}).get("mac", "") or cfg.get("parent", {}).get("mac", "")
    if is_valid_mac(hub_mac) and hub_mac not in ("00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff"):
        _paired = True
        return True
    return False

def init_espnow_client(on_cmd_received_fn=None):
    global _e, _stop_requested
    if not has_espnow:
        print(" ESP-NOW not supported on this firmware build.")
        return None

    cfg = config.load_config()
    _stop_requested = False
    client_name = cfg.get("client", {}).get("custom_name", "Client Node")
    print(f" Initializing {client_name} ESP-NOW Client...")
    
    # Deactivate AP interface to force ESP-NOW to bind to STA interface
    try:
        ap = network.WLAN(network.AP_IF)
        ap.active(False)
    except:
        pass
        
    ch = cfg.get("wifi", {}).get("channel", 4)
    set_wifi_channel(ch)
    
    _e = espnow.ESPNow()
    _e.active(True)
    try:
        _e.config(rxbuf=4096)
    except:
        pass
    
    espnow_relay.init_relay_engine(_e, lambda next_hop_bytes, payload_bytes, phys_mac, target_id: tx_queue.put((next_hop_bytes, payload_bytes, phys_mac, target_id)))
    
    # Only request pairing if no valid parent/Hub MAC is stored in configuration
    if not is_paired():
        send_pairing_request()
    else:
        hub_mac = cfg.get("hub", {}).get("mac", "") or cfg.get("parent", {}).get("mac", "")
        print(f" Node paired with Hub/Parent {hub_mac} on Channel {ch}")
        
    return _e

def client_listen_loop(heartbeats=None, on_cmd_received_fn=None):
    global _e, _paired, _last_hub_rx_time
    if _e is None:
        return

    sta = network.WLAN(network.STA_IF)
    local_mac = bytes_to_mac(sta.config('mac'))

    cfg = config.load_config()
    client_cfg = cfg.get("client", {})
    local_id = client_cfg.get("id", "").lower()

    _last_hub_rx_time = time.time()
    recv_buffers = {}
    recv_last_seen = {}

    while not _stop_requested:
        if heartbeats is not None:
            heartbeats["esp_now"] = time.time()

        # Fallback to scanning if we lose contact with our paired Hub for 45s
        if _paired and time.time() - _last_hub_rx_time > 45:
            print(" Lost contact with Hub for 45s. Re-entering scanning mode...")
            _paired = False

        if not _paired:
            current_cfg = config.load_config()
            parent_mac = current_cfg.get("parent", {}).get("mac", "00:00:00:00:00:00")
            time_since_last_rx = time.time() - _last_hub_rx_time
            
            if parent_mac == "00:00:00:00:00:00" or time_since_last_rx > 20:
                send_discovery_request()
            else:
                send_pairing_request()

        try:
            host, msg = _e.recv(500)
            if host and msg:
                # Ignore tiny non-JSON fragments that can appear during ESP-NOW
                # fragmentation and poison the receive buffer.
                if len(msg) < 8 and b'{' not in msg:
                    continue

                sender_mac = bytes_to_mac(host)
                # Cleanup stale peer buffer if contact gap > 10s
                now_t = time.time()
                if now_t - recv_last_seen.get(sender_mac, now_t) > 10:
                    recv_buffers[sender_mac] = b""
                recv_last_seen[sender_mac] = now_t

                buf = recv_buffers.get(sender_mac, b"") + msg
                recv_buffers[sender_mac] = buf

                while True:
                    payload_bytes, remainder = extract_complete_frame(recv_buffers[sender_mac])
                    if payload_bytes is None:
                        if len(recv_buffers[sender_mac]) > MAX_RX_BUFFER:
                            recv_buffers[sender_mac] = b""
                        break

                    recv_buffers[sender_mac] = remainder
                    try:
                        payload_str = payload_bytes.decode('utf-8')
                    except Exception as decode_err:
                        print(f"  Ignoring non-UTF-8 payload from {sender_mac}: {decode_err}")
                        continue
                    print(f" Received packet from {sender_mac}: {payload_str}")

                    packet = parse_packet(payload_str)
                    
                    # Sync RTC if envelope has a valid Hub timestamp
                    hub_ts = packet.get("timestamp") or packet.get("ts")
                    if hub_ts and isinstance(hub_ts, (int, float)) and hub_ts > 1700000000:
                        local_ts = config.get_unix_time()
                        if abs(local_ts - hub_ts) > 5:
                            try:
                                import machine
                                tm = time.gmtime(hub_ts - 946684800)
                                rtc = machine.RTC()
                                rtc.datetime((tm[0], tm[1], tm[2], tm[6], tm[3], tm[4], tm[5], 0))
                                print(f" RTC synchronized via ESP-NOW from Hub to: {time.localtime()}")
                            except Exception as sync_ex:
                                print(" Failed to sync RTC via ESP-NOW:", sync_ex)

                    msg_type = packet.get("msg_type")
                    target = packet.get("target")

                    is_for_us = False
                    if target:
                        t_lower = target.lower()
                        is_for_us = (t_lower == local_id or t_lower in ("broadcast", "ff:ff:ff:ff:ff:ff"))
                    else:
                        is_for_us = (msg_type == "CMD" or msg_type == "ACK" or msg_type == "COMMAND")

                    # Handle BEACON packets
                    if msg_type == "BEACON":
                        b_pld = packet.get("data") or packet.get("pld", {})
                        hub_mac = b_pld.get("hub_mac", sender_mac)
                        parent_mac = b_pld.get("sender_mac", sender_mac)
                        b_ch = b_pld.get("channel")

                        current_cfg = config.load_config()
                        paired_hub = current_cfg.get("hub", {}).get("mac", "")
                        is_our_hub = (hub_mac.lower().replace(':', '') == paired_hub.lower().replace(':', ''))

                        if not _paired or (is_our_hub and b_ch and current_cfg.get("wifi", {}).get("channel") != b_ch):
                            print(f" Received BEACON from {'paired ' if _paired else ''}Hub {hub_mac} (Channel: {b_ch})")
                            _last_hub_rx_time = time.time()
                            try:
                                upd = {"hub": {"mac": hub_mac}, "parent": {"mac": parent_mac}}
                                if b_ch:
                                    upd["wifi"] = {"channel": b_ch}
                                    set_wifi_channel(b_ch)
                                config.update_config(upd)
                            except Exception as b_ex:
                                print("Error handling BEACON:", b_ex)

                    if msg_type == "ACK" and is_for_us:
                        ack_pld = packet.get("data") or packet.get("pld", {})
                        if ack_pld.get("status") == "paired":
                            _paired = True
                            _last_hub_rx_time = time.time()
                            hub_mac = ack_pld.get("hub_mac", sender_mac)
                            hub_ch = ack_pld.get("channel")
                            print(f"Client paired successfully with Hub ({hub_mac}) on Channel {hub_ch}!")
                            try:
                                upd = {"hub": {"mac": hub_mac}, "parent": {"mac": hub_mac}}
                                if hub_ch:
                                    upd["wifi"] = {"channel": hub_ch}
                                    set_wifi_channel(hub_ch)
                                config.update_config(upd)
                            except Exception as ex:
                                print("Error updating config on pairing:", ex)

                    # Handle Route/Parent Discovery packets
                    if msg_type == "DISCOVERY_REQ":
                        current_cfg = config.load_config()
                        if _paired and (time.time() - _last_hub_rx_time < 60):
                            sender_mac = bytes_to_mac(host)
                            hub_mac = current_cfg.get("hub", {}).get("mac", "")
                            channel = current_cfg.get("wifi", {}).get("channel", 6)
                            
                            parent_mac = current_cfg.get("parent", {}).get("mac", "")
                            hop_count = 1 if parent_mac == hub_mac else 2
                            
                            resp_payload = {
                                "status": "discovery_response",
                                "hub_mac": hub_mac,
                                "parent_mac": local_mac,
                                "channel": channel,
                                "hop_count": hop_count,
                                "hub_freshness": int(time.time() - _last_hub_rx_time)
                            }
                            
                            print(f" Received DISCOVERY_REQ from {sender_mac}. Replying with DISCOVERY_RESP...")
                            send_direct_espnow(
                                target_mac_str=sender_mac,
                                target_id=packet.get("source") or sender_mac,
                                msg_type="DISCOVERY_RESP",
                                payload=resp_payload
                            )
                        continue

                    if msg_type == "DISCOVERY_RESP" and is_for_us:
                        resp_data = packet.get("data") or packet.get("pld", {})
                        hub_mac = resp_data.get("hub_mac")
                        parent_mac = resp_data.get("parent_mac")
                        ch = resp_data.get("channel")
                        hop_count = resp_data.get("hop_count")
                        hub_freshness = resp_data.get("hub_freshness", 999)
                        
                        print(f" Received DISCOVERY_RESP from {sender_mac} (Hub={hub_mac}, Channel={ch}, Hops={hop_count})")
                        
                        try:
                            upd = {
                                "hub": {"mac": hub_mac},
                                "parent": {"mac": parent_mac},
                                "wifi": {"channel": ch}
                            }
                            config.update_config(upd)
                            set_wifi_channel(ch)
                            
                            _last_hub_rx_time = time.time() - hub_freshness
                            print(f" Discovered route: parent={parent_mac}, hub={hub_mac}, locked to channel {ch}")
                        except Exception as ex:
                            print(" Error saving discovered route:", ex)
                        continue

                    # Relaying and target validation
                    is_actually_for_us = espnow_relay.process_and_relay(packet)

                    # Update RX timestamp if packet is from Hub
                    current_cfg = config.load_config()
                    paired_hub = current_cfg.get("hub", {}).get("mac", "")
                    if sender_mac.lower().replace(':', '') == paired_hub.lower().replace(':', ''):
                        _last_hub_rx_time = time.time()

                    if is_actually_for_us and (msg_type == "COMMAND" or msg_type == "CMD") and on_cmd_received_fn is not None:
                        payload = packet.get("data") or packet.get("pld", {})
                        cmd = payload.get("cmd") or payload.get("command")

                        payload["sender_mac"] = sender_mac
                        payload["routing_path"] = packet.get("hops", [])

                        on_cmd_received_fn(cmd, payload)
            else:
                time.sleep_ms(50)

        except Exception as err:
            err_str = str(err)
            if "buffer error" in err_str:
                try:
                    _e.active(False)
                except:
                    pass
                time.sleep_ms(50)
                try:
                    _e = espnow.ESPNow()
                    _e.active(True)
                except:
                    pass
            else:
                print("Client loop error:", err)
            time.sleep_ms(100)

    try:
        _e.active(False)
    except Exception:
        pass
    print(" ESP-NOW listener stopped")
