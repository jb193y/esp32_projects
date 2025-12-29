import ujson

CONFIG_FILE = 'config.json'

DEFAULT_CONFIG = {
    "device_id": "esp32_gps_01",
    "mode": "sta",
    "wifi_networks": [
        {"ssid": "JayMobile", "password": "2147742366"},
        {"ssid": "ITSHERE", "password": "2147742366"}
    ],
    "mqtt_server": "wwww.uxpreon.com",
    "mqtt_port": 1883
}

def load_config():
    try:
        with open(CONFIG_FILE, 'r') as f:
            return ujson.load(f)
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
