#!/usr/bin/env python3
"""
Dual Serial Monitor Utility
Monitors two ESP32 serial COM ports simultaneously with prefixed, timestamped,
and color-coded log output.

Usage:
    python utils/dual_serial_monitor.py [PORT1] [PORT2]
    python utils/dual_serial_monitor.py --hub COM20 --vc COM21
    python utils/dual_serial_monitor.py COM20 COM21 --baud 115200
"""

import sys
import time
import argparse
import threading
import serial

# ANSI Color codes for terminal distinction
COLOR_HUB = "\033[96m"   # Cyan
COLOR_NODE = "\033[93m"  # Yellow
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
        except (serial.SerialException, PermissionError, OSError) as conn_err:
            time.sleep(1)
        finally:
            if ser and ser.is_open:
                try:
                    ser.close()
                except Exception:
                    pass
        time.sleep(0.5)

def main():
    parser = argparse.ArgumentParser(description="Dual ESP32 Serial Monitor")
    parser.add_argument("ports", nargs="*", default=[], help="Serial ports (e.g. COM20 COM21)")
    parser.add_argument("--hub", default=None, help="Hub serial port (e.g. COM20)")
    parser.add_argument("--vc", "--node", default=None, dest="node", help="Valve Controller / Node serial port (e.g. COM21)")
    parser.add_argument("--baud", type=int, default=115200, help="Baud rate (default: 115200)")

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

    print("=" * 60)
    print(f"  Dual Serial Monitor: {port1} (HUB) <==> {port2} (NODE)")
    print(f"  Baud rate: {args.baud} | Press Ctrl+C to stop")
    print("=" * 60)

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

    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print(f"\n{COLOR_INFO}Stopping Dual Serial Monitor...{COLOR_RESET}")
        stop_event.set()
        time.sleep(0.5)

if __name__ == "__main__":
    main()
