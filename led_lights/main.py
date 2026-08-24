import sys
import time
import machine
import network
import ujson
import ntptime
import _thread

import config
import led_status
from light_manager import LightManager
import factory_reset

try:
    from umqtt.simple import MQTTClient
    has_mqtt = True
except ImportError:
    has_mqtt = False

# Global state
light_manager = None
mqtt_client = None
is_wifi_connected = False
is_mqtt_connected = False

def connect_wifi(wifi_cfg):
    global is_wifi_connected
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    
    networks = wifi_cfg.get("networks", [])
    if not networks:
        print("No Wi-Fi networks configured.")
        return False

    net = networks[0]
    ssid = net.get("ssid")
    password = net.get("password", "")
    
    print(f"Connecting to Wi-Fi SSID: {ssid}...")
    led_status.set_status("WIFI_CONNECTING")
    
    wlan.connect(ssid, password)
    
    # Wait up to 15 seconds
    for _ in range(30):
        if wlan.isconnected():
            is_wifi_connected = True
            print("Wi-Fi connected successfully! IP Info:", wlan.ifconfig())
            led_status.set_status("WIFI_CONNECTED")
            return True
        time.sleep_ms(500)
        
    print("Failed to connect to Wi-Fi.")
    led_status.set_status("WIFI_FAILED")
    return False

def sync_time():
    print("Syncing time via NTP...")
    for attempt in range(5):
        try:
            ntptime.settime()
            lt = time.localtime()
            # Apply offset if configured (e.g., local time conversions)
            print(f"NTP Time Sync completed. Current local time: {lt}")
            return True
        except Exception as e:
            print(f"NTP Sync attempt {attempt + 1} failed: {e}")
            time.sleep(2)
    return False

def on_message(topic, msg):
    global light_manager
    try:
        topic_str = topic.decode('utf-8')
        payload_str = msg.decode('utf-8')
        print(f"MQTT Command Received: Topic={topic_str}, Payload={payload_str}")
        
        payload = ujson.loads(payload_str)
        data = payload.get("data", {})
        
        # 1. Color / Pattern commands
        if "color" in data or "pattern" in data or "power" in data or "speed" in data:
            power = data.get("power")
            color = data.get("color")
            pattern = data.get("pattern")
            speed = data.get("speed")
            light_manager.set_color_pattern(power=power, color=color, pattern=pattern, speed=speed)
            
        # 2. Schedule configuration commands
        if "schedule" in data:
            schedule_data = data.get("schedule", {})
            light_manager.save_schedule(schedule_data)
            
    except Exception as e:
        print("Error parsing MQTT payload:", e)

def connect_mqtt(mqtt_cfg, client_id):
    global mqtt_client, is_mqtt_connected
    if not has_mqtt:
        print("umqtt library is not available.")
        return False
        
    server = mqtt_cfg.get("server", "192.168.1.100")
    port = mqtt_cfg.get("port", 1883)
    user = mqtt_cfg.get("user")
    password = mqtt_cfg.get("password")
    
    print(f"Connecting to MQTT Broker: {server}:{port}...")
    led_status.set_status("MQTT_CONNECTING")
    
    try:
        mqtt_client = MQTTClient(
            client_id=client_id,
            server=server,
            port=port,
            user=user,
            password=password,
            keepalive=60
        )
        mqtt_client.set_callback(on_message)
        mqtt_client.connect()
        is_mqtt_connected = True
        print("MQTT Broker connected successfully!")
        
        # Subscribe to commands
        sub_topic = f"ledlights/{client_id}/command"
        mqtt_client.subscribe(sub_topic.encode('utf-8'))
        print(f"Subscribed to topic: {sub_topic}")
        led_status.set_status("MQTT_CONNECTED")
        return True
    except Exception as e:
        print("MQTT connection failed:", e)
        led_status.set_status("MQTT_FAILED")
        return False

def main():
    global light_manager, mqtt_client, is_wifi_connected, is_mqtt_connected
    
    # Configure default thread stack size to 8KB to prevent overflow
    try:
        _thread.stack_size(8192)
        print("Default thread stack size configured to 8KB")
    except Exception as ex:
        print("Failed to configure thread stack size:", ex)

    # Initialize Factory Reset monitor (GPIO 0 & 47)
    try:
        factory_reset.start()
    except Exception as fr_ex:
        print("Failed to start Factory Reset monitor:", fr_ex)

    # 1. Load system config
    cfg = config.load_config()
    client_info = cfg.get("client", {})
    client_id = client_info.get("id", "ledlights_01")
    mode = client_info.get("mode", "provisioning")
    
    print(f"Booting LED Lights Node: {client_id} (Mode: {mode})")
    
    # 2. If in provisioning mode, run BLE pairing
    if mode in ("provisioning", "ble_setup"):
        try:
            import ble_manager
            led_status.set_status("BLE_PROVISIONING")
            ble_manager.start_provisioning()
        except Exception as e:
            print("BLE Provisioning manager failed to start:", e)
            # Fallback to AP or loop to avoid crash
            while True:
                time.sleep(1)
        return

    # 3. Normal Mode: Initialize Lights & Connect
    led_cfg = cfg.get("led", {})
    led_pin = led_cfg.get("pin", 48)
    num_pixels = led_cfg.get("num_pixels", 1)
    
    light_manager = LightManager(pin_num=led_pin, num_pixels=num_pixels)
    
    wifi_cfg = cfg.get("wifi", {})
    mqtt_cfg = cfg.get("mqtt", {})
    
    # Connect Wifi & MQTT
    if connect_wifi(wifi_cfg):
        sync_time()
        connect_mqtt(mqtt_cfg, client_id)
        
    last_retry_time = time.ticks_ms()
    
    print("LED Lights main execution loop started.")
    
    # Main execution loop
    while True:
        try:
            # Check wifi connection
            wlan = network.WLAN(network.STA_IF)
            if not wlan.isconnected():
                if is_wifi_connected:
                    print("Wi-Fi disconnected! Reconnecting...")
                    is_wifi_connected = False
                    is_mqtt_connected = False
                    led_status.set_status("WIFI_CONNECTING")
                
                # Try reconnecting every 15 seconds
                now = time.ticks_ms()
                if time.ticks_diff(now, last_retry_time) > 15000:
                    last_retry_time = now
                    if connect_wifi(wifi_cfg):
                        sync_time()
                        connect_mqtt(mqtt_cfg, client_id)
            
            # Check MQTT connection
            elif not is_mqtt_connected:
                now = time.ticks_ms()
                if time.ticks_diff(now, last_retry_time) > 15000:
                    last_retry_time = now
                    connect_mqtt(mqtt_cfg, client_id)
            
            # Process incoming MQTT messages
            if is_mqtt_connected and mqtt_client:
                try:
                    mqtt_client.check_msg()
                except Exception as mqtt_err:
                    print("MQTT message poll failed:", mqtt_err)
                    is_mqtt_connected = False
                    try:
                        mqtt_client.disconnect()
                    except:
                        pass
            
            # Run animations and schedules
            if light_manager:
                light_manager.update()
                
            time.sleep_ms(20)
            
        except KeyboardInterrupt:
            print("Loop interrupted by keyboard. Exiting...")
            break
        except Exception as loop_err:
            print("Error in main loop execution:", loop_err)
            time.sleep(1)

if __name__ == "__main__":
    main()
