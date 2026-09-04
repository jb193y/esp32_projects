import config

def build_espnow_envelope(source, destination, msg_type, payload, route_id="direct", hops=None):
    """
    Format a standard ESP-NOW routing envelope.
    """
    if hops is None:
        hops = [destination]
    return {
        "src": source,
        "dst": destination,
        "t": msg_type,
        "ts": int(config.get_unix_time()),
        "rt": {
            "rid": route_id,
            "h": hops
        },
        "pld": payload
    }

def build_mqtt_payload(source, target, msg_type, data, route_transport="ESPNOW", route_id="direct", current_hop_index=0, hops=None, timestamp=None, msg_id=None):
    """
    Format a standard MQTT wrapper payload adhering to the unified IoT protocol.
    """
    if hops is None:
        hops = []
    if timestamp is None:
        timestamp = int(config.get_unix_time())
    if msg_id is None:
        msg_id = str(timestamp)
    
    envelope = {
        "id": msg_id,
        "source": source,
        "target": target,
        "type": msg_type,
        "ts": timestamp,
        "route": {
            "transport": route_transport,
            "route_id": route_id,
            "current_hop_index": current_hop_index,
            "hops": hops,
            "link_diagnostics": []
        }
    }
    
    if msg_type in ("COMMAND", "CMD"):
        envelope["action"] = data
    elif msg_type == "CONFIG":
        envelope["config"] = data
    elif msg_type == "ACK":
        envelope["ack"] = data
    else:  # TELEMETRY, STATUS, PROVISIONING
        envelope["state"] = data
        
    return envelope