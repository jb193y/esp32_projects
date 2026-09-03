#!/usr/bin/env python3
"""
Dual Serial Monitor Utility + Live MQTT Monitor
Monitors two ESP32 serial COM ports simultaneously alongside real-time MQTT broker
messages with prefixed, timestamped, and color-coded log output.

Usage:
    python utils/dual_serial_monitor.py COM20 COM21
    python utils/dual_serial_monitor.py COM20 COM21 -mqtt_sub_true
    python utils/dual_serial_monitor.py --hub COM20 --vc COM21 --mqtt-sub --mqtt-host 10.10.10.211
"""

import sys
import time
import socket
import argparse
import threading
import serial

# ANSI Color codes for terminal distinction
COLOR_HUB = "\033[96m"   # Cyan
COLOR_NODE = "\033[93m"  # Yellow
COLOR_MQTT = "\033[95m"  # Magenta
COLOR_RESET = "\033[0m"
COLOR_ERR = "\033[91m"   # Red
COLOR_INFO = "\033[92m"  # Green

def read_serial_worker(port, name, color, baud=115200, stop_event=None):
    while not stop_event.is_set():
        ser = None
        try:
            ser = serial.Serial(port, baud, timeout=1)
            print(f"{COLOR_INFO}[{name}] Connected to {port} at {baud} baud{COLOR_RESET}")
            sys.stdout.flush()
            
            while not stop_event.is_set():
                try:
                    line = ser.readline()
                    if line:
                        decoded = line.decode('utf-8', errors='replace').rstrip()
                        if decoded:
                            timestamp = time.strftime("%H:%M:%S")
                            print(f"{color}[{timestamp}][{name}-{port}]{COLOR_RESET} {decoded}")
                            sys.stdout.flush()
                except (serial.SerialException, OSError) as read_err:
                    print(f"{COLOR_ERR}[{name}] Disconnected ({read_err}). Reconnecting...{COLOR_RESET}")
                    break
        except (serial.SerialException, PermissionError, OSError):
            time.sleep(1)
        finally:
            if ser and ser.is_open:
                try:
                    ser.close()
                except Exception:
                    pass
        time.sleep(0.5)

def encode_varlen(n):
    res = bytearray()
    while True:
        b = n % 128
        n = n // 128
        if n > 0:
            b |= 0x80
        res.append(b)
        if n == 0:
            break
    return bytes(res)

def recv_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)

def decode_varlen(sock):
    multiplier = 1
    value = 0
    while True:
        b = recv_exact(sock, 1)
        if not b:
            return None
        byte_val = b[0]
        value += (byte_val & 127) * multiplier
        if (byte_val & 128) == 0:
            break
        multiplier *= 128
    return value

def mqtt_monitor_worker(host, port, user, password, topics, stop_event):
    """Pure-Python standard library MQTT 3.1.1 Subscriber (Zero dependencies)."""
    import select
    while not stop_event.is_set():
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(10.0)
            sock.connect((host, port))
            
            # 1. Build MQTT 3.1.1 CONNECT Packet
            client_id = f"dual_mon_{int(time.time()*1000)%100000}".encode('utf-8')
            u_bytes = user.encode('utf-8') if user else b""
            p_bytes = password.encode('utf-8') if password else b""
            
            connect_flags = 0x02  # Clean session
            if user:
                connect_flags |= 0x80
            if password:
                connect_flags |= 0x40

            var_header = bytearray(b"\x00\x04MQTT\x04")
            var_header.append(connect_flags)
            var_header.extend(b"\x00\x3c")  # Keepalive 60s
            
            payload = bytearray()
            payload.extend(len(client_id).to_bytes(2, 'big') + client_id)
            if user:
                payload.extend(len(u_bytes).to_bytes(2, 'big') + u_bytes)
            if password:
                payload.extend(len(p_bytes).to_bytes(2, 'big') + p_bytes)
                
            connect_packet = b"\x10" + encode_varlen(len(var_header) + len(payload)) + var_header + payload
            sock.sendall(connect_packet)
            
            connack = recv_exact(sock, 4)
            if not connack or len(connack) < 4 or connack[3] != 0:
                print(f"{COLOR_ERR}[MQTT] Connection rejected by broker{COLOR_RESET}")
                sock.close()
                time.sleep(3)
                continue
                
            print(f"{COLOR_INFO}[MQTT] Connected to {host}:{port}. Subscribed to: {', '.join(topics)}{COLOR_RESET}")
            sys.stdout.flush()
            
            sub_payload = bytearray()
            for t in topics:
                t_bytes = t.encode('utf-8')
                sub_payload.extend(len(t_bytes).to_bytes(2, 'big') + t_bytes + b"\x00")
            
            packet_id = 1
            sub_var_header = packet_id.to_bytes(2, 'big')
            sub_packet = b"\x82" + encode_varlen(len(sub_var_header) + len(sub_payload)) + sub_var_header + sub_payload
            sock.sendall(sub_packet)

            sock.settimeout(5.0)
            last_ping = time.time()
            
            while not stop_event.is_set():
                if time.time() - last_ping > 25:
                    try:
                        sock.sendall(b"\xc0\x00")  # PINGREQ
                        last_ping = time.time()
                    except Exception:
                        break

                r, _, _ = select.select([sock], [], [], 0.5)
                if not r:
                    continue

                header = sock.recv(1)
                if not header:
                    break
                pkt_type = header[0] >> 4
                rem_len = decode_varlen(sock)
                if rem_len is None:
                    break
                    
                raw_body = recv_exact(sock, rem_len)
                if not raw_body:
                    break
                    
                if pkt_type == 3:  # PUBLISH
                    qos = (header[0] >> 1) & 0x03
                    topic_len = int.from_bytes(raw_body[0:2], 'big')
                    topic = raw_body[2:2+topic_len].decode('utf-8', errors='replace')
                    payload_offset = 2 + topic_len
                    if qos > 0:
                        payload_offset += 2  # Skip Packet ID
                    msg_payload = raw_body[payload_offset:].decode('utf-8', errors='replace')
                    
                    timestamp = time.strftime("%H:%M:%S")
                    print(f"{COLOR_MQTT}[{timestamp}][MQTT]{COLOR_RESET} Topic={topic}, Payload={msg_payload}")
                    sys.stdout.flush()
                elif pkt_type == 9:  # SUBACK
                    pass
                elif pkt_type == 13:  # PINGRESP
                    pass
        except Exception as e:
            pass
        finally:
            if sock:
                try:
                    sock.close()
                except Exception:
                    pass
        time.sleep(2)

def main():
    parser = argparse.ArgumentParser(description="Dual ESP32 Serial Monitor + MQTT Live Sub")
    parser.add_argument("ports", nargs="*", default=[], help="Serial ports (e.g. COM20 COM21)")
    parser.add_argument("--hub", default=None, help="Hub serial port (e.g. COM20)")
    parser.add_argument("--vc", "--node", default=None, dest="node", help="Valve Controller / Node serial port (e.g. COM21)")
    parser.add_argument("--baud", type=int, default=115200, help="Baud rate (default: 115200)")
    
    # MQTT options
    parser.add_argument("-mqtt_sub_true", "--mqtt_sub_true", "--mqtt-sub", dest="mqtt_sub", action="store_true",
                        help="Enable live MQTT topic subscription monitor")
    parser.add_argument("--mqtt-host", default="10.10.10.211", help="MQTT broker IP (default: 10.10.10.211)")
    parser.add_argument("--mqtt-port", type=int, default=1883, help="MQTT broker port (default: 1883)")
    parser.add_argument("--mqtt-topic", "-t", action="append", default=None, help="MQTT topic pattern to monitor (can specify multiple)")
    parser.add_argument("--mqtt-user", default="mss_client", help="MQTT username (default: mss_client)")
    parser.add_argument("--mqtt-pass", default="Xgs7%67$!@#_", help="MQTT password")

    args = parser.parse_args()

    port1 = args.hub
    port2 = args.node

    if len(args.ports) >= 2:
        port1 = args.ports[0]
        port2 = args.ports[1]
    elif len(args.ports) == 1:
        if not port1:
            port1 = args.ports[0]
        elif not port2:
            port2 = args.ports[0]

    # Defaults if not specified
    if not port1:
        port1 = "COM20"
    if not port2:
        port2 = "COM21"

    topics = args.mqtt_topic or ["+/+/+/+/+", "+/+/+/+", "farm/#"]

    print("=" * 65)
    print(f"  Dual Serial Monitor: {port1} (HUB) <==> {port2} (NODE)")
    if args.mqtt_sub:
        print(f"  MQTT Monitor Active: {args.mqtt_host}:{args.mqtt_port} -> {', '.join(topics)}")
    print(f"  Baud rate: {args.baud} | Press Ctrl+C to stop")
    print("=" * 65)

    stop_event = threading.Event()
    
    t1 = threading.Thread(
        target=read_serial_worker,
        args=(port1, "HUB", COLOR_HUB, args.baud, stop_event),
        daemon=True
    )
    t2 = threading.Thread(
        target=read_serial_worker,
        args=(port2, "NODE", COLOR_NODE, args.baud, stop_event),
        daemon=True
    )

    t1.start()
    t2.start()

    if args.mqtt_sub:
        t_mqtt = threading.Thread(
            target=mqtt_monitor_worker,
            args=(args.mqtt_host, args.mqtt_port, args.mqtt_user, args.mqtt_pass, topics, stop_event),
            daemon=True
        )
        t_mqtt.start()

    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print(f"\n{COLOR_INFO}Stopping Monitor...{COLOR_RESET}")
        stop_event.set()
        time.sleep(0.5)

if __name__ == "__main__":
    main()
