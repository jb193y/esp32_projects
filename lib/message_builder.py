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
            "route_id": route_id,
            "hops": hops
        },
        "pld": payload
    }

def build_mqtt_payload(source, target, msg_type, data, route_transport="ESPNOW", route_id="direct", current_hop_index=0, hops=None, timestamp=None):
    """
    Format a standard MQTT wrapper payload.
    """
    if hops is None:
        hops = []
    if timestamp is None:
        timestamp = int(config.get_unix_time())
    return {
        "source": source,
        "target": target,
        "msg_type": msg_type,
        "timestamp": timestamp,
        "route": {
            "transport": route_transport,
            "route_id": route_id,
            "current_hop_index": current_hop_index,
            "hops": hops,
            "link_diagnostics": []
        },
        "data": data
    }