import espnow
import network
import time


def mac_to_bytes(mac_str):
    return bytes(int(x, 16) for x in mac_str.split(':'))


def bytes_to_mac(mac_bytes):
    return ':'.join('%02x' % b for b in mac_bytes)


def make_frame(body):
    payload = body.encode('utf-8')
    return len(payload).to_bytes(2, 'big') + payload


# Replace this with the actual MAC address of espcom_a.
PEER_MAC = "3c:dc:75:6e:8f:64"

sta = network.WLAN(network.STA_IF)
sta.active(True)
sta.config(channel=6)

ap = network.WLAN(network.AP_IF)
ap.active(False)

_e = espnow.ESPNow()
_e.active(True)

peer = mac_to_bytes(PEER_MAC)
try:
    _e.add_peer(peer)
    print("Peer added:", PEER_MAC)
except Exception as e:
    print("add_peer failed:", e)

print("B is listening for ESP-NOW traffic...")
recv_buf = b""

while True:
    try:
        host, incoming = _e.recv(1000)
        if host and incoming:
            sender = bytes_to_mac(host)
            recv_buf += incoming
            while len(recv_buf) >= 2:
                frame_len = int.from_bytes(recv_buf[:2], 'big')
                total_len = 2 + frame_len
                if len(recv_buf) < total_len:
                    break
                frame = recv_buf[2:total_len]
                recv_buf = recv_buf[total_len:]
                payload = frame.decode('utf-8', 'ignore')
                print("B received from:", sender)
                print("B payload:", payload)
                try:
                    reply = f"ACK:{payload}"
                    _e.send(peer, make_frame(reply))
                    print("B replied:", reply)
                except Exception as e:
                    print("Reply error:", e)
    except Exception as e:
        print("Recv error:", e)

    time.sleep_ms(50)
