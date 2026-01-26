# gps.py
import _thread
import machine
import time
import math
import struct
from lib.micropyGPS import MicropyGPS
import config
from imu import is_moving, imu_accel_vector  # kept as in your current file

# -----------------------------
# LOAD CONFIG
# -----------------------------
cfg = config.load_config()

client_cfg = cfg.get("client", {})
gps_cfg = cfg.get("gps", {})

CLIENT_TYPE = client_cfg.get("type", "rover")  # "rover" or "base"

# base-only (validated/used only when base)
base_cfg = cfg.get("base", {})
KNOWN_LAT = base_cfg.get("known_lat")
KNOWN_LON = base_cfg.get("known_lon")

# -----------------------------
# GPS Settings
# -----------------------------
GPS_UART_ID = gps_cfg.get("uart_id", 2)
GPS_BAUD = gps_cfg.get("baud", 9600)
GPS_TX = gps_cfg.get("tx", 17)
GPS_RX = gps_cfg.get("rx", 16)

# -----------------------------
# GPS Processing Settings
# -----------------------------
HDOP_MAX = gps_cfg.get("hdop_max", 3.0)                 # accept fixes only if hdop <= this (set 99 to disable)
AVG_BUF = gps_cfg.get("avg_buf", 8)                     # moving average window
KALMAN_Q = gps_cfg.get("kf_q", 1e-6)                    # process noise
KALMAN_R = gps_cfg.get("kf_r", 1e-4)                    # measurement noise
STATIONARY_SPEED_KMH = gps_cfg.get("stationary_speed_kmh", 0.8)
STATIONARY_METERS = gps_cfg.get("stationary_meters", 2.0)
STATIONARY_COUNT_LOCK = gps_cfg.get("stationary_count_lock", 6)

# -----------------------------
# UART + GPS Parser
# -----------------------------
gps_uart = machine.UART(GPS_UART_ID, baudrate=GPS_BAUD, tx=GPS_TX, rx=GPS_RX)
gps = MicropyGPS(location_formatting='dd')  # dd => [decimal, 'N'] / [decimal, 'W']

# Shared state for other threads
gps_data = {
    "lat": None,
    "lon": None,
    "timestamp": None,
    "hdop": None,
    "sats": None,
    "speed_kmh": None,
    "confidence_m": None,
    "locked": False,
}
gps_raw_data = {
    "timestamp": None,
    "nmea_sentences": [],
}
lock = _thread.allocate_lock()

# -----------------------------
# Helpers
# -----------------------------
def iso_timestamp():
    t = time.localtime()
    return "%04d-%02d-%02dT%02d:%02d:%02dZ" % (t[0], t[1], t[2], t[3], t[4], t[5])

def haversine_m(lat1, lon1, lat2, lon2):
    R = 6371000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = (math.sin(dphi/2)**2) + math.cos(phi1) * math.cos(phi2) * (math.sin(dlmb/2)**2)
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
    return R * c

def safe_float(x):
    try:
        return float(x)
    except:
        return None

def get_hdop():
    try:
        h = gps.hdop
        if isinstance(h, (list, tuple)):
            return safe_float(h[0])
        return safe_float(h)
    except:
        return None

def get_speed_kmh():
    try:
        s = gps.speed
        if isinstance(s, (list, tuple)) and len(s) > 0:
            return safe_float(s[0])
        return safe_float(s)
    except:
        return None

def estimate_confidence_m(hdop, sats):
    if hdop is None:
        return None
    base = 5.0 * hdop
    if sats is not None and sats < 5:
        base *= 1.5
    return base

class Kalman1D:
    def __init__(self, q=KALMAN_Q, r=KALMAN_R):
        self.q = q
        self.r = r
        self.x = None
        self.p = 1.0

    def update(self, z):
        if self.x is None:
            self.x = z
            return z
        self.p = self.p + self.q
        k = self.p / (self.p + self.r)
        self.x = self.x + k * (z - self.x)
        self.p = (1 - k) * self.p
        return self.x

def compute_correction(measured_lat, measured_lon, known_lat, known_lon):
    return {
        "delta_lat": known_lat - measured_lat,
        "delta_lon": known_lon - measured_lon
    }

# -----------------------------
# UBX Configuration Helpers
# -----------------------------
def calc_checksum(payload):
    ck_a, ck_b = 0, 0
    for b in payload:
        ck_a = (ck_a + b) & 0xFF
        ck_b = (ck_b + ck_a) & 0xFF
    return ck_a, ck_b

def send_ubx(cls, msg_id, payload):
    """Sends a UBX message to the GPS module."""
    # Frame: [Sync1][Sync2][Class][ID][Len][Payload][CK_A][CK_B]
    length = len(payload)
    content = struct.pack("<BBH", cls, msg_id, length) + payload
    ck_a, ck_b = calc_checksum(content)
    packet = b"\xB5\x62" + content + struct.pack("BB", ck_a, ck_b)
    gps_uart.write(packet)

def configure_gps_module():
    print("⚙️ Configuring GPS Module (UBX)...")
    
    # 1. Dynamic Model & Min Elevation (CFG-NAV5)
    # -------------------------------------------
    # Determine Model
    model_cfg = gps_cfg.get("dynamic_model", "").lower()
    if "stationary" in model_cfg:
        dyn_model = 2
    elif "automotive" in model_cfg:
        dyn_model = 4
    elif "pedestrian" in model_cfg:
        dyn_model = 3
    else:
        # Default based on Client Type
        dyn_model = 2 if CLIENT_TYPE == "base" else 3 # Pedestrian for rover default
    
    min_elev = int(gps_cfg.get("min_elevation", 15))
    
    print(f"   - Dynamic Model: {dyn_model} (2=Stat, 3=Ped, 4=Auto)")
    print(f"   - Min Elevation: {min_elev}°")

    # Payload for CFG-NAV5 (36 bytes)
    # Mask: 0x0003 (Apply DynModel and MinEl settings)
    payload = struct.pack("<HBBiiBbHHHHBBBBHHB5s", 
        0x0003,      # mask
        dyn_model,   # dynModel
        3,           # fixMode (3 = Auto 2D/3D)
        0,           # fixedAlt
        10000,       # fixedAltVar
        min_elev,    # minElev
        0,           # drLimit
        500, 500,    # pDop, tDop
        100, 100,    # pAcc, tAcc
        0, 60,       # staticHoldThresh, dgpsTimeOut
        0, 0,        # cno
        0, 0, 0,     # reserved, staticHoldMaxDist, utcStandard
        b'\x00'*5    # reserved
    )
    send_ubx(0x06, 0x24, payload)
    time.sleep(0.1)

    # 2. Update Rate (CFG-RATE)
    # -------------------------
    rate_hz = int(gps_cfg.get("update_rate_hz", 1))
    if rate_hz > 5: rate_hz = 5 # Limit to 5Hz for stability
    if rate_hz < 1: rate_hz = 1
    
    meas_rate = 1000 // rate_hz
    print(f"   - Update Rate: {rate_hz}Hz ({meas_rate}ms)")
    
    # measRate(2), navRate(2), timeRef(2)
    payload = struct.pack("<HHH", meas_rate, 1, 1) # 1 = GPS Time
    send_ubx(0x06, 0x08, payload)
    time.sleep(0.1)

# -----------------------------
# GPS Thread
# -----------------------------
def gps_thread(heartbeats=None): # Add 'heartbeats=None' here
    lat_buf, lon_buf = [], []
    k_lat = Kalman1D()
    k_lon = Kalman1D()

    last_filtered = {"lat": None, "lon": None}
    stationary_count = 0
    locked_pos = {"lat": None, "lon": None}

    last_fix_process = time.time()
    
    # NMEA sentence capture
    nmea_buffer = ""

    # Validation (base must have known coords)
    if CLIENT_TYPE == "base":
        if KNOWN_LAT is None or KNOWN_LON is None:
            print("⚠️ BASE mode but base.known_lat/known_lon missing — corrections will not work.")

    print("✅ GPS thread started")
    configure_gps_module()

    def smooth(buf, v):
        buf.append(v)
        if len(buf) > AVG_BUF:
            buf.pop(0)
        return sum(buf) / len(buf)

    while True:
        if heartbeats:
            heartbeats["gps"] = time.time() # This feeds the Watchdog
        while gps_uart.any():
            try:
                c = gps_uart.read(1)
                if c:
                    char = c.decode("utf-8", "ignore")
                    gps.update(char)
                    # Capture raw NMEA sentence
                    if char == '\r':
                        continue
                    elif char == '\n':
                        if nmea_buffer and nmea_buffer.startswith('$'):
                            lock.acquire()
                            try:
                                gps_raw_data["nmea_sentences"].append(nmea_buffer)
                                # Keep only last 20 sentences
                                if len(gps_raw_data["nmea_sentences"]) > 20:
                                    gps_raw_data["nmea_sentences"].pop(0)
                                gps_raw_data["timestamp"] = iso_timestamp()
                            finally:
                                lock.release()
                        nmea_buffer = ""
                    else:
                        nmea_buffer += char
            except:
                pass

        now = time.time()
        if now - last_fix_process >= 1:
            last_fix_process = now

            # dd format check
            if not (gps.latitude and gps.longitude and len(gps.latitude) == 2 and len(gps.longitude) == 2):
                time.sleep(0.02)
                continue

            lat = safe_float(gps.latitude[0])
            lon = safe_float(gps.longitude[0])
            if lat is None or lon is None or lat == 0 or lon == 0:
                time.sleep(0.02)
                continue

            # Hemisphere
            if gps.latitude[1] == "S":
                lat = -lat
            if gps.longitude[1] == "W":
                lon = -lon

            hdop = get_hdop()
            try:
                sats = int(gps.satellites_in_use)
            except:
                sats = None
            speed_kmh = get_speed_kmh()

            # HDOP filter
            if hdop is not None and hdop > HDOP_MAX:
                time.sleep(0.02)
                continue

            # Average + Kalman
            lat = smooth(lat_buf, lat)
            lon = smooth(lon_buf, lon)
            lat = k_lat.update(lat)
            lon = k_lon.update(lon)

            # Stationary lock (jitter freeze)
            locked = False
            if last_filtered["lat"] is not None:
                d = haversine_m(last_filtered["lat"], last_filtered["lon"], lat, lon)
            else:
                d = None

            stationary_now = False
            if speed_kmh is not None:
                stationary_now = (speed_kmh <= STATIONARY_SPEED_KMH)
            if d is not None:
                stationary_now = stationary_now or (d <= STATIONARY_METERS)

            if stationary_now:
                stationary_count += 1
            else:
                stationary_count = 0
                locked_pos["lat"], locked_pos["lon"] = None, None

            if stationary_count >= STATIONARY_COUNT_LOCK:
                if locked_pos["lat"] is None:
                    locked_pos["lat"], locked_pos["lon"] = lat, lon
                lat, lon = locked_pos["lat"], locked_pos["lon"]
                locked = True

            last_filtered["lat"], last_filtered["lon"] = lat, lon
            confidence_m = estimate_confidence_m(hdop, sats)

            # Publish shared data
            lock.acquire()
            data_to_print = None
            try:
                gps_data["lat"] = lat
                gps_data["lon"] = lon
                gps_data["timestamp"] = iso_timestamp()
                gps_data["hdop"] = hdop
                gps_data["sats"] = sats
                gps_data["speed_kmh"] = speed_kmh
                gps_data["confidence_m"] = confidence_m
                gps_data["locked"] = locked
                data_to_print = dict(gps_data)
            finally:
                lock.release()
            if data_to_print:
                print("GPS:", data_to_print)

        time.sleep(0.02)
