import ujson
import ntptime
import time
import machine

CONFIG_FILE = 'config.json'
_MIN_VALID_EPOCH = 1700000000  # ~2023 sanity check

DEFAULT_CONFIG = {
    "client_id": "esp32_gps_01",
    "mode": "sta",
    "app_type": "rover",
    "base_id": "base_01",
    "known_lat": 33.106000,
    "known_lon": -96.633000,
    # WiFi Networks
    "wifi_networks": [
        {"ssid": "ITSHERE", "password": ""},
        {"ssid": "JayMobile", "password": ""}
    ],
    # Time / NTP
    "ntp": {
        "enabled": True,
        "server": "pool.ntp.org",
        "timezone_offset": 0,   # UTC = 0
        "sync_on_boot": True
    },
  "time": {
    "last_epoch": 0
  },
    "mqtt_server": "10.10.10.211",
    "mqtt_port": 1883,
    "publish_every_sec": 10,
    "move_threshold_m": 5.0,
    "mqtt_max_retries": 5,
    "mqtt_retry_delay": 5,
    "hdop_max": 3.0,
    "gps_uart_id": 2,
    "gps_baud": 9600,
    "gps_tx": 17,
    "gps_rx": 16,
    "gps_avg_buf": 8,
    "kf_process_noise": 1e-6,
    "kf_measurement_noise": 1e-4,
    "stationary_speed_kmh": 0.8,
    "stationary_meters": 2.0,
    "stationary_count_lock": 6,
    "OTA": {
    "base_url": "http://10.10.10.211:8000/fw"
}
}

def load_config():
    try:
        # with open(CONFIG_FILE, 'r') as f:
        #     return ujson.load(f)
        save_config(DEFAULT_CONFIG)
        return DEFAULT_CONFIG
    except Exception:
        save_config(DEFAULT_CONFIG)
        return DEFAULT_CONFIG

def save_config(cfg):
    with open(CONFIG_FILE, 'w') as f:
        ujson.dump(cfg, f)

def update_config(data):
    cfg = load_config()
    cfg.update(data)
    save_config(cfg)
    return cfg

# ----------------------------
# Time Persistence
# ----------------------------
def restore_time(cfg):
    epoch = cfg.get("time", {}).get("last_epoch", 0)

    if epoch > _MIN_VALID_EPOCH:
        rtc = machine.RTC()
        rtc.datetime(time.gmtime(epoch))
        print("⏪ Time restored from flash:", time.gmtime())
        return True

    print("⚠️ No valid time in config")
    return False


def save_time(cfg):
    epoch = time.time()

    if epoch > _MIN_VALID_EPOCH:
        cfg.setdefault("time", {})["last_epoch"] = int(epoch)
        save_config(cfg)
        print("💾 Time saved to config.json")


# ----------------------------
# NTP Sync
# ----------------------------
def sync_time_ntp(cfg, retries=3):
    if not cfg.get("ntp", {}).get("enabled"):
        return False

    ntptime.host = cfg["ntp"].get("server", "pool.ntp.org")

    for _ in range(retries):
        try:
            ntptime.settime()
            save_time(cfg)
            print("🌐 Time synced via NTP")
            return True
        except Exception as e:
            print("❌ NTP failed:", e)
            time.sleep(2)

    return False


# ----------------------------
# MQTT Time Sync
# ----------------------------
def set_time_from_epoch(cfg, epoch):
    if epoch > _MIN_VALID_EPOCH:
        rtc = machine.RTC()
        rtc.datetime(time.gmtime(epoch))
        cfg.setdefault("time", {})["last_epoch"] = int(epoch)
        save_config(cfg)
        print("🕒 Time set via MQTT (epoch)")


def set_time_from_iso(cfg, ts):
    # "2026-01-01T14:30:00Z"
    try:
        rtc = machine.RTC()
        rtc.datetime((
            int(ts[0:4]), int(ts[5:7]), int(ts[8:10]), 0,
            int(ts[11:13]), int(ts[14:16]), int(ts[17:19]), 0
        ))
        save_time(cfg)
        print("🕒 Time set via MQTT (ISO)")
    except Exception as e:
        print("❌ Invalid ISO timestamp:", e)


# ----------------------------
# Timestamp Helper
# ----------------------------
def utc_iso():
    t = time.gmtime()
    return "%04d-%02d-%02dT%02d:%02d:%02dZ" % t[:6]