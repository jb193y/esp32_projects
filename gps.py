# gps.py (only top config section changed)
import _thread
import machine
import time
import math
from lib.micropyGPS import MicropyGPS
import config
from imu import is_moving, imu_accel_vector  # (as in your current file)

cfg = config.load_config()
gps_cfg = cfg.get("gps", {})
app_cfg = cfg.get("app", {})
mqtt_cfg = cfg.get("mqtt", {})

HDOP_MAX = gps_cfg.get("hdop_max", 3.0)
AVG_BUF = gps_cfg.get("avg_buf", 8)
KALMAN_Q = gps_cfg.get("kf_q", 1e-6)
KALMAN_R = gps_cfg.get("kf_r", 1e-4)
STATIONARY_SPEED_KMH = gps_cfg.get("stationary_speed_kmh", 0.8)
STATIONARY_METERS = gps_cfg.get("stationary_meters", 2.0)
STATIONARY_COUNT_LOCK = gps_cfg.get("stationary_count_lock", 6)

APP_TYPE = app_cfg.get("type", "rover")
KNOWN_LAT = app_cfg.get("known_lat", None)
KNOWN_LON = app_cfg.get("known_lon", None)

GPS_UART_ID = gps_cfg.get("uart_id", 2)
GPS_BAUD = gps_cfg.get("baud", 9600)
GPS_TX = gps_cfg.get("tx", 17)
GPS_RX = gps_cfg.get("rx", 16)

gps_uart = machine.UART(GPS_UART_ID, baudrate=GPS_BAUD, tx=GPS_TX, rx=GPS_RX)
gps = MicropyGPS(location_formatting='dd')

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
lock = _thread.allocate_lock()

# -----------------------------
# Helpers
# -----------------------------
def iso_timestamp():
    t = time.localtime()
    return "%04d-%02d-%02dT%02d:%02d:%02dZ" % (t[0], t[1], t[2], t[3], t[4], t[5])

def haversine_m(lat1, lon1, lat2, lon2):
    # meters
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
    # MicropyGPS variants: sometimes gps.hdop is float, sometimes list/tuple
    try:
        h = gps.hdop
        if isinstance(h, (list, tuple)):
            return safe_float(h[0])
        return safe_float(h)
    except:
        return None

def get_speed_kmh():
    # MicropyGPS often stores speed as [value, unit] or tuple
    try:
        s = gps.speed
        if isinstance(s, (list, tuple)) and len(s) > 0:
            return safe_float(s[0])
        return safe_float(s)
    except:
        return None

def estimate_confidence_m(hdop, sats):
    # Very rough rule-of-thumb: ~5m * HDOP under open sky
    if hdop is None:
        return None
    base = 5.0 * hdop
    # If very low sats, inflate slightly
    if sats is not None and sats < 5:
        base *= 1.5
    return base

def normalize(x, y):
    mag = math.sqrt(x*x + y*y)
    if mag == 0:
        return 0, 0
    return x / mag, y / mag

def direction_correlates(imu_ax, imu_ay, gps_dx, gps_dy):
    imu_x, imu_y = normalize(imu_ax, imu_ay)
    gps_x, gps_y = normalize(gps_dx, gps_dy)

    dot = imu_x * gps_x + imu_y * gps_y
    return dot > -0.2   # tolerate some noise


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
        # predict
        self.p = self.p + self.q
        # update
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
# GPS Thread
# -----------------------------
def gps_thread():
    lat_buf, lon_buf = [], []
    k_lat = Kalman1D()
    k_lon = Kalman1D()

    last_filtered = {"lat": None, "lon": None}
    stationary_count = 0
    locked_pos = {"lat": None, "lon": None}

    last_fix_process = time.time()

    print("✅ GPS thread started")

    def smooth(buf, v):
        buf.append(v)
        if len(buf) > AVG_BUF:
            buf.pop(0)
        return sum(buf) / len(buf)

    while True:
        # Read GPS chars fast
        if gps_uart.any():
            try:
                c = gps_uart.read(1)
                if c:
                    gps.update(c.decode('utf-8', 'ignore'))
            except:
                pass

        # Process fix at ~1Hz to avoid over-processing noise
        now = time.time()
        if now - last_fix_process >= 1:
            last_fix_process = now

            # Valid dd format check
            if not (gps.latitude and gps.longitude and len(gps.latitude) == 2 and len(gps.longitude) == 2):
                time.sleep(0.02)
                continue

            lat = safe_float(gps.latitude[0])
            lon = safe_float(gps.longitude[0])
            if lat is None or lon is None or lat == 0 or lon == 0:
                time.sleep(0.02)
                continue

            # Apply hemisphere
            if gps.latitude[1] == 'S':
                lat = -lat
            if gps.longitude[1] == 'W':
                lon = -lon

            # Quality signals
            hdop = get_hdop()
            sats = None
            try:
                sats = int(gps.satellites_in_use)
            except:
                sats = None
            speed_kmh = get_speed_kmh()

            # HDOP filter (skip bad fixes)
            if hdop is not None and hdop > HDOP_MAX:
                # don’t overwrite last good gps_data; just skip this noisy sample
                time.sleep(0.02)
                continue

            # Moving average
            lat = smooth(lat_buf, lat)
            lon = smooth(lon_buf, lon)

            # Kalman filter (extra stability)
            lat = k_lat.update(lat)
            lon = k_lon.update(lon)

            # Stationary detection
            locked = False
            if last_filtered["lat"] is not None:
                d = haversine_m(last_filtered["lat"], last_filtered["lon"], lat, lon)
            else:
                d = None

            # Use speed if available, else distance-only
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
                # lock position to stop jitter
                if locked_pos["lat"] is None:
                    locked_pos["lat"], locked_pos["lon"] = lat, lon
                lat, lon = locked_pos["lat"], locked_pos["lon"]
                locked = True

            last_filtered["lat"], last_filtered["lon"] = lat, lon

            confidence_m = estimate_confidence_m(hdop, sats)

            # Publish shared data
            lock.acquire()
            try:
                gps_data["lat"] = lat
                gps_data["lon"] = lon
                gps_data["timestamp"] = iso_timestamp()
                gps_data["hdop"] = hdop
                gps_data["sats"] = sats
                gps_data["speed_kmh"] = speed_kmh
                gps_data["confidence_m"] = confidence_m
                gps_data["locked"] = locked
            finally:
                lock.release()

        # yield CPU (important for Wi-Fi/MQTT)
        time.sleep(0.02)
