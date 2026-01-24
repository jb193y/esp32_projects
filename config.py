# config.py
import ujson
import time
import machine
import ntptime
import os

CONFIG_FILE = "config.json"
DEFAULT_FILE = "config.defaults.json"

MIN_VALID_EPOCH = 1700000000  # ~2023 sanity check


# ----------------------------
# JSON helpers
# ----------------------------
def _read_json(path):
    with open(path, "r") as f:
        return ujson.load(f)


def _write_json(path, obj):
    with open(path, "w") as f:
        ujson.dump(obj, f)


def _deep_merge(dst, src):
    """
    Fill missing keys in dst from src (recursive for dicts).
    Does NOT overwrite existing keys in dst.
    """
    for k, v in src.items():
        if k not in dst:
            dst[k] = v
        else:
            if isinstance(dst[k], dict) and isinstance(v, dict):
                _deep_merge(dst[k], v)
    return dst


def _ensure_filesystem_defaults():
    # If defaults file is missing, that's a deployment problem.
    # We fail loudly so you notice.
    if DEFAULT_FILE not in os.listdir():
        raise OSError("Missing %s (factory defaults). Upload it with the firmware." % DEFAULT_FILE)


# ----------------------------
# Config API
# ----------------------------
def load_defaults():
    _ensure_filesystem_defaults()
    return _read_json(DEFAULT_FILE)


def load_config():
    """
    Loads config.json (user/runtime) and merges config.defaults.json into it (fill missing only).
    If config.json is missing/corrupt, it will be created from defaults.
    """
    defaults = load_defaults()

    cfg = {}
    try:
        if CONFIG_FILE in os.listdir():
            cfg = _read_json(CONFIG_FILE)
    except Exception:
        cfg = {}

    if not isinstance(cfg, dict):
        cfg = {}

    # Fill missing keys
    cfg = _deep_merge(cfg, defaults)

    # Ensure runtime file exists and is repaired
    try:
        _write_json(CONFIG_FILE, cfg)
    except Exception:
        pass

    return cfg


def save_config(cfg):
    _write_json(CONFIG_FILE, cfg)


def update_config(data):
    """
    Shallow update at top-level groups only:
      client/wifi/mqtt/gps/app/time/ota/server/imu
    Values should be dicts.
    """
    cfg = load_config()
    if not isinstance(data, dict):
        return cfg

    for group, patch in data.items():
        if isinstance(patch, dict) and isinstance(cfg.get(group), dict):
            cfg[group].update(patch)
        else:
            # allow adding entirely new group (rare)
            cfg[group] = patch

    save_config(cfg)
    return cfg


# ----------------------------
# Time Persistence
# ----------------------------
def restore_time(cfg):
    epoch = 0
    try:
        epoch = int(cfg.get("time", {}).get("last_epoch", 0) or 0)
    except Exception:
        epoch = 0

    if epoch > MIN_VALID_EPOCH:
        rtc = machine.RTC()
        rtc.datetime(time.gmtime(epoch))
        print("⏪ Time restored from flash:", time.gmtime())
        return True

    print("⚠️ No valid time in config")
    return False


def save_time(cfg):
    epoch = time.time()
    if epoch > MIN_VALID_EPOCH:
        cfg.setdefault("time", {})["last_epoch"] = int(epoch)
        save_config(cfg)
        print("💾 Time saved to config.json")


# ----------------------------
# NTP Sync
# ----------------------------
def sync_time_ntp(cfg, retries=3):
    ntp_cfg = cfg.get("time", {}).get("ntp", {})
    if not ntp_cfg.get("enabled", False):
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
    try:
        epoch = int(epoch)
    except Exception:
        return

    if epoch > MIN_VALID_EPOCH:
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


def utc_iso():
    t = time.gmtime()
    return "%04d-%02d-%02dT%02d:%02d:%02dZ" % t[:6]
