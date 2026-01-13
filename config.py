# config.py
import ujson
import ntptime
import time
import machine
import os

CONFIG_FILE = "config.json"
_MIN_VALID_EPOCH = 1700000000  # ~2023 sanity check

DEFAULT_CONFIG = {
    "device": {
        "client_id": "esp32_gps_01",
        "mode": "sta",  # "sta" or "ap"
    },
    "app": {
        "type": "rover",     # "base" or "rover"
        "base_id": "base_01",
        "known_lat": 33.106000,
        "known_lon": -96.633000,
    },
    "wifi": {
        "networks": [
            {"ssid": "ITSHERE", "password": "2147742366"},
            {"ssid": "JayMobile", "password": "2147742366"},
        ]
    },
    "time": {
        "last_epoch": 0,
        "ntp": {
            "enabled": True,
            "server": "pool.ntp.org",
            "timezone_offset": 0,  # keep UTC
            "sync_on_boot": True,
        },
    },
    "mqtt": {
        "server": "10.10.10.211",
        "port": 1883,
        "publish_every_sec": 10,
        "move_threshold_m": 5.0,
        "max_retries": 5,
        "retry_delay": 5,
    },
    "gps": {
        "uart_id": 2,
        "baud": 9600,
        "tx": 17,
        "rx": 16,
        "avg_buf": 8,
        "hdop_max": 3.0,
        "kf_q": 1e-6,
        "kf_r": 1e-4,
        "stationary_speed_kmh": 0.8,
        "stationary_meters": 2.0,
        "stationary_count_lock": 6,
    },
    "imu": {
        "enabled": True
    },
    "ota": {
        "base_url": "http://10.10.10.211:8000/fw",
        "manifest": "manifest.json",
        "auto_apply": False
    },
    "server": {
        "enabled": True,
        "port": 80,
        "ap_ssid": "ESP32_Setup",
        "ap_password": "12345678"
    }
}

def _deep_merge(dst, src):
    # Merge src into dst (dicts), fill missing defaults only.
    for k, v in src.items():
        if k not in dst:
            dst[k] = v
        else:
            if isinstance(dst[k], dict) and isinstance(v, dict):
                _deep_merge(dst[k], v)
    return dst

def _read_json(path):
    with open(path, "r") as f:
        return ujson.load(f)

def save_config(cfg):
    with open(CONFIG_FILE, "w") as f:
        ujson.dump(cfg, f)

def load_config():
    cfg = None
    try:
        if CONFIG_FILE in os.listdir():
            cfg = _read_json(CONFIG_FILE)
        else:
            cfg = {}
    except Exception:
        cfg = {}

    # Fill missing defaults
    cfg = _deep_merge(cfg, DEFAULT_CONFIG)

    # If config file didn't exist or was broken, persist repaired version
    try:
        save_config(cfg)
    except Exception:
        pass

    return cfg

def update_config(data):
    """
    Shallow update at top-level keys only (device/wifi/mqtt/gps/ota/etc).
    Mobile app should send grouped objects.
    """
    cfg = load_config()
    for k, v in data.items():
        if isinstance(v, dict) and isinstance(cfg.get(k), dict):
            cfg[k].update(v)
        else:
            cfg[k] = v
    save_config(cfg)
    return cfg

# ----------------------------
# Time Persistence
# ----------------------------
def restore_time(cfg):
    epoch = cfg.get("time", {}).get("last_epoch", 0)
    if epoch and epoch > _MIN_VALID_EPOCH:
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
    ntp_cfg = cfg.get("time", {}).get("ntp", {})
    if not ntp_cfg.get("enabled"):
        return False

    ntptime.host = ntp_cfg.get("server", "pool.ntp.org")

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

def utc_iso():
    t = time.gmtime()
    return "%04d-%02d-%02dT%02d:%02d:%02dZ" % t[:6]
