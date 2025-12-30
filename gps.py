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
gps_raw_data = ""
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
                c = gps_uart.read(1).decode('utf-8', 'ignore')
                gps.update(c)
            except Exception:
                continue

            time.sleep(0.2)  # Allow buffer to fill
            lock.acquire()
            gps_raw_data="RAW GPS:" + str(gps.latitude) + " " + str(gps.longitude) + " fix:" + str(gps.fix_stat) + " sats:" + str(gps.satellites_in_use)
            lock.release()
            print(gps_raw_data)
            # Check for GPS fix
            # if gps.fix_stat >= 1:
            if (
                gps.latitude[0] != 0 and
                gps.longitude[0] != 0 and
                gps.latitude[2] in ('N', 'S') and
                gps.longitude[2] in ('E', 'W')
            ):
                print("GPS fix acquired")
                time.sleep(1)  # Allow buffer to fill
                try:
                    # Ensure latitude/longitude arrays are complete
                    if len(gps.latitude) < 3 or len(gps.longitude) < 3:
                        continue
                    
                    print("Raw GPS data:", gps.latitude, gps.longitude)
                    # Clean the numeric values
                    lat_deg = float(str(gps.latitude[0]).strip())
                    lat_min = float(str(gps.latitude[1]).strip())
                    lon_deg = float(str(gps.longitude[0]).strip())
                    lon_min = float(str(gps.longitude[1]).strip())

                    lat = lat_deg + lat_min / 60.0
                    lon = lon_deg + lon_min / 60.0

                    if gps.latitude[2] == 'S':
                        lat = -lat
                    if gps.longitude[2] == 'W':
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

                except Exception as e:
                    print("GPS parse error:", e)

        time.sleep(0.1)
# ------------------------------------------------------------------
# END OF FILE