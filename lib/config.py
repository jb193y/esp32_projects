# config.py (Shared Configuration Library)
import ujson
import os

CONFIG_FILE = "config.json"
DEFAULT_FILE = "config.defaults.json"

_cached_config = None

def _read_json(path):
    with open(path, "r") as f:
        return ujson.load(f)

def _write_json(path, obj):
    with open(path, "w") as f:
        ujson.dump(obj, f)

def _deep_merge(dst, src):
    for k, v in src.items():
        if k not in dst:
            dst[k] = v
        else:
            if isinstance(dst[k], dict) and isinstance(v, dict):
                _deep_merge(dst[k], v)
    return dst

def load_defaults():
    if DEFAULT_FILE not in os.listdir():
        return {}
    return _read_json(DEFAULT_FILE)

def load_config():
    global _cached_config
    if _cached_config is not None:
        return _cached_config

    defaults = load_defaults()
    cfg = {}
    try:
        cfg = _read_json(CONFIG_FILE)
    except Exception:
        cfg = {}

    if not isinstance(cfg, dict):
        cfg = {}

    cfg = _deep_merge(cfg, defaults)
    _cached_config = cfg
    return cfg

def save_config(cfg):
    global _cached_config
    _cached_config = cfg
    _write_json(CONFIG_FILE, cfg)

def update_config(data):
    cfg = load_config()
    if not isinstance(data, dict):
        return cfg

    for group, patch in data.items():
        if isinstance(patch, dict) and isinstance(cfg.get(group), dict):
            cfg[group].update(patch)
        else:
            cfg[group] = patch

    save_config(cfg)
    return cfg
