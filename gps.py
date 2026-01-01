import _thread
import machine
import time
from micropyGPS import MicropyGPS

# ------------------------------------------------------------------
# INITIALIZATION
# ------------------------------------------------------------------
gps_uart = machine.UART(2, baudrate=9600, tx=17, rx=16)
gps = MicropyGPS(location_formatting='dd')

gps_data = {"lat": None, "lon": None, "timestamp": None}
lock = _thread.allocate_lock()

# ------------------------------------------------------------------
# ISO 8601 TIMESTAMP (MicroPython-compatible)
# ------------------------------------------------------------------
def iso_timestamp():
    t = time.localtime()
    # (year, month, day, hour, minute, second, weekday, yearday)
    return "%04d-%02d-%02dT%02d:%02d:%02dZ" % (t[0], t[1], t[2], t[3], t[4], t[5])

# ------------------------------------------------------------------
# GPS READING THREAD
# ------------------------------------------------------------------
def gps_thread():
    global gps_data
    global gps_raw_data

    buf_size = 5
    lat_buf, lon_buf = [], []

    def smooth(buf, val):
        buf.append(val)
        if len(buf) > buf_size:
            buf.pop(0)
        return sum(buf) / len(buf)

    print("GPS thread started")

    while True:
        if gps_uart.any():
            try:
                # Read ONE character at a time (required by MicropyGPS)
                c = gps_uart.read(1)
                if c:
                    gps.update(c.decode('utf-8', 'ignore'))
            except Exception:
                pass

        # Check for GPS fix (works for dd formatting)
        if (
            gps.latitude and gps.longitude and
            len(gps.latitude) == 2 and
            len(gps.longitude) == 2 and
            gps.latitude[0] != 0 and
            gps.longitude[0] != 0
        ):
            try:
                lat = gps.latitude[0]
                lon = gps.longitude[0]

                if gps.latitude[1] == 'S':
                    lat = -lat
                if gps.longitude[1] == 'W':
                    lon = -lon

                lat = smooth(lat_buf, lat)
                lon = smooth(lon_buf, lon)

                lock.acquire()
                gps_data = {
                    "lat": lat,
                    "lon": lon,
                    "timestamp": iso_timestamp()
                }
                lock.release()

                print("📍 GPS:", lat, lon)

                # ✅ GPS updates at 1Hz → no need to recompute faster
                time.sleep(0.9)

            except Exception as e:
                print("GPS parse error:", e)

        # ✅ Yield CPU (VERY IMPORTANT on ESP32)
        time.sleep(0.02)

# ------------------------------------------------------------------
# END OF FILE