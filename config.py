import ujson

CONFIG_FILE = 'config.json'

DEFAULT_CONFIG = {
    "client_id": "esp32_gps_01",
    "mode": "sta",
    "wifi_networks": [
        {"ssid": "ITSHERE", "password": "2147742366"},
        {"ssid": "JayMobile", "password": "2147742366"}
    ],
    "mqtt_server": "10.10.10.211",
    "mqtt_port": 1883,
    "publish_every_sec": 10,
    "move_threshold_m": 5.0,
    "mqtt_max_retries": 5,
    "mqtt_retry_delay": 5
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
    "stationary_count_lock": 6
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
