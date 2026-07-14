# display_manager.py
import time
import machine
import _thread
import network
import pump_controller
import config
import gc

# Color Definitions (RGB565)
BLACK    = 0x0000
WHITE    = 0xFFFF
GREEN    = 0x07E0
CYAN     = 0x07FF
YELLOW   = 0xFFE0
RED      = 0xF800
BLUE     = 0x001F
GRAY     = 0x7BEF
DARKGRAY = 0x2104
ORANGE   = 0xFD20
DARKRED  = 0x8000

# Display dimensions
W, H = 320, 240

# Globals
tft = None
spi = None
running = False

def init_display():
    global tft, spi
    cfg = config.load_config()
    disp_cfg = cfg.get("display", {})
    
    if not disp_cfg.get("enabled", True):
        print("📺 Display is disabled in configuration.")
        return False
        
    pins_cfg = disp_cfg.get("pins", {})
    dc_pin = pins_cfg.get("dc", 39)
    sck_pin = pins_cfg.get("sck", 41)
    mosi_pin = pins_cfg.get("mosi", 40)
    rst_pin = pins_cfg.get("rst", 42)
    cs_pin = pins_cfg.get("cs", 47)
    bl_pin = pins_cfg.get("backlight", 46)
    
    print(f"📺 Initializing ST7789 display (CS={cs_pin}, DC={dc_pin}, SCK={sck_pin}, MOSI={mosi_pin})")
    
    try:
        # Enable Backlight pin if it exists
        if bl_pin is not None:
            bl = machine.Pin(bl_pin, machine.Pin.OUT)
            bl.value(1)
            
        # Hardware SPI 2
        spi = machine.SPI(
            2,
            baudrate=disp_cfg.get("baudrate", 40_000_000),
            polarity=1,
            phase=1,
            sck=machine.Pin(sck_pin),
            mosi=machine.Pin(mosi_pin),
            miso=None
        )
        
        # Import local ST7789 driver
        from st7789 import ST7789
        
        tft = ST7789(
            spi,
            disp_cfg.get("width", 320),
            disp_cfg.get("height", 240),
            reset=machine.Pin(rst_pin, machine.Pin.OUT),
            dc=machine.Pin(dc_pin, machine.Pin.OUT),
            cs=machine.Pin(cs_pin, machine.Pin.OUT),
            rotation=disp_cfg.get("rotation", 1),
            xstart=disp_cfg.get("xstart", 0),
            ystart=disp_cfg.get("ystart", 0)
        )
        
        tft.fill(BLACK)
        print("📺 Display initialization successful.")
        return True
    except Exception as e:
        print("🚨 Display initialization failed:", e)
        return False

def draw_setup_portal(cfg):
    # 1. Header Banner
    tft.fill_rect(0, 0, 320, 30, BLUE)
    tft.text("BLE PAIRING ACTIVE", 10, 11, WHITE, BLUE)
    
    # 2. Divider Line
    tft.fill_rect(0, 30, 320, 1, WHITE)
    
    # 3. Setup Body
    tft.fill_rect(0, 35, 320, 160, BLACK)
    tft.text("To configure this device:", 10, 45, WHITE, BLACK)
    
    tft.text("1. Open mobile app", 10, 75, CYAN, BLACK)
    tft.text("2. Scan for BLE devices", 10, 100, CYAN, BLACK)
    
    client_id = cfg.get("client", {}).get("id", "esp32_pump_01")
    dev_name = f"{client_id}_Setup"
    
    tft.text("3. Connect to BLE device:", 10, 125, CYAN, BLACK)
    tft.text(f"   Name: {dev_name}", 10, 145, YELLOW, BLACK)
    tft.text("4. Send Wi-Fi & MQTT setup data", 10, 170, CYAN, BLACK)
    
    # 4. Footer
    tft.fill_rect(0, 195, 320, 45, BLACK)
    tft.fill_rect(0, 198, 320, 1, GRAY)
    
    try:
        import ble_provisioning
        connected = ble_provisioning.is_connected()
    except:
        connected = False
        
    if connected:
        tft.text("BLE STATUS: CONNECTED!", 10, 212, GREEN, BLACK)
    else:
        tft.text("WAITING FOR BLE CONNECTION...", 10, 212, YELLOW, BLACK)

ota_status = None
last_command = None
last_command_time = 0

def set_ota_status(status):
    global ota_status
    ota_status = status

def show_command_toast(cmd_name):
    global last_command, last_command_time
    last_command = cmd_name
    last_command_time = time.time()

def draw_ota_screen(status):
    # 1. Header Banner
    tft.fill_rect(0, 0, 320, 30, RED)
    tft.text("OTA FIRMWARE UPDATE", 10, 11, WHITE, RED)
    
    # 2. Divider Line
    tft.fill_rect(0, 30, 320, 1, WHITE)
    
    # 3. Body
    tft.fill_rect(0, 35, 320, 160, BLACK)
    tft.text("FIRMWARE UPGRADE IN PROGRESS", 10, 60, YELLOW, BLACK)
    tft.text(status, 10, 95, WHITE, BLACK)
    
    tft.text("WARNING: DO NOT POWER OFF!", 10, 140, RED, BLACK)
    
    # 4. Footer
    tft.fill_rect(0, 195, 320, 45, BLACK)
    tft.fill_rect(0, 198, 320, 1, GRAY)
    tft.text("DEVICE WILL REBOOT ON COMPLETE", 10, 212, GREEN, BLACK)

def draw_dashboard():
    gc.collect()
    if tft is None:
        return
        
    cfg = config.load_config()
    
    # Check OTA status override
    global ota_status
    if ota_status is not None:
        draw_ota_screen(ota_status)
        return
        
    client_mode = cfg.get("client", {}).get("mode", "ap")
    if client_mode in ["ap", "ble_setup"]:
        draw_setup_portal(cfg)
        return
        
    # Read variables safely under pump controller lock
    pump_controller.lock.acquire()
    try:
        state = pump_controller.state
        telemetry = dict(pump_controller.telemetry)
        active_faults = list(pump_controller.active_faults)
        sim_mode = pump_controller.SIMULATE
    finally:
        pump_controller.lock.release()
        
    # Query WiFi status
    sta_if = network.WLAN(network.STA_IF)
    ap_if = network.WLAN(network.AP_IF)
    
    if sta_if.isconnected():
        wifi_status = "STA"
        wifi_color = GREEN
        ip_addr = sta_if.ifconfig()[0]
    elif ap_if.active():
        wifi_status = "AP"
        wifi_color = CYAN
        ip_addr = ap_if.ifconfig()[0]
    else:
        wifi_status = "DISC"
        wifi_color = RED
        ip_addr = "0.0.0.0"

    # 1. Header Banner
    banner_color = DARKGRAY
    banner_text = f"PUMP SYSTEM - {state}"
    text_color = WHITE
    
    if state == "RUNNING":
        banner_color = GREEN
        text_color = BLACK
    elif state == "STARTING":
        banner_color = YELLOW
        text_color = BLACK
    elif state == "RESTART_DELAY":
        banner_color = ORANGE
        text_color = BLACK
    elif state == "TRIPPED":
        banner_color = RED
        text_color = WHITE
        
    tft.fill_rect(0, 0, 320, 30, banner_color)
    tft.text(banner_text, 10, 11, text_color, banner_color)
    
    # 2. Divider Line
    tft.fill_rect(0, 30, 320, 1, WHITE)
    
    # 3. Telemetry Rows
    # Row 1 (Y=40): Voltages average & Operation Mode
    tft.fill_rect(0, 35, 320, 45, BLACK) # Clear telemetry area 1
    tft.text(f"AVG V: {telemetry.get('v_avg', 0.0)} V", 10, 40, CYAN, BLACK)
    tft.text(f"MODE: {telemetry.get('mode', 'MANUAL')}", 180, 40, GREEN if telemetry.get('mode') == 'AUTO' else YELLOW, BLACK)
    
    # Row 2 (Y=55): Individual Phase Voltages
    tft.text(f"Va:{telemetry.get('v_a', 0.0)} Vb:{telemetry.get('v_b', 0.0)} Vc:{telemetry.get('v_c', 0.0)}", 10, 58, WHITE, BLACK)
    
    # Row 3 (Y=80): Currents average & Water Tank Status
    tft.fill_rect(0, 80, 320, 45, BLACK) # Clear telemetry area 2
    tft.text(f"AVG I: {telemetry.get('i_avg', 0.00)} A", 10, 80, CYAN, BLACK)
    tft.text(f"TANK: {telemetry.get('tank_level_str', 'MID')}", 180, 80, WHITE, BLACK)
    
    # Row 4 (Y=95): Individual Phase Currents
    tft.text(f"Ia:{telemetry.get('i_a', 0.00)} Ib:{telemetry.get('i_b', 0.00)} Ic:{telemetry.get('i_c', 0.00)}", 10, 98, WHITE, BLACK)
    
    # Row 5 (Y=120): Pressure & Water Flow Rate
    tft.fill_rect(0, 120, 320, 25, BLACK) # Clear telemetry area 3
    tft.text(f"PRESS: {telemetry.get('pressure', 0.0)} PSI", 10, 122, CYAN, BLACK)
    tft.text(f"FLOW: {telemetry.get('flow_rate', 0.0)} GPM", 180, 122, CYAN, BLACK)
    
    # Row 6 (Y=145): Accumulated Energy Statistics
    tft.fill_rect(0, 145, 320, 25, BLACK) # Clear telemetry area 4
    # Calculate running hours from runtime_sec
    hrs = round(pump_controller.runtime_sec / 3600.0, 1)
    kwh = round(pump_controller.est_kwh, 2)
    tft.text(f"EST_KWH: {kwh}", 10, 145, WHITE, BLACK)
    tft.text(f"RUN: {hrs} HRS", 180, 145, WHITE, BLACK)
    
    # Query MQTT connection status
    try:
        import mqtt
        mqtt_ok = mqtt.is_connected
    except:
        mqtt_ok = False
        
    mqtt_status = "CONN" if mqtt_ok else "DISC"
    mqtt_color = GREEN if mqtt_ok else RED

    # Row 7 (Y=170): WiFi, MQTT & IP configurations
    tft.fill_rect(0, 170, 320, 25, BLACK) # Clear telemetry area 5
    tft.text(f"WIFI:{wifi_status}", 10, 170, wifi_color, BLACK)
    tft.text(f"MQTT:{mqtt_status}", 105, 170, mqtt_color, BLACK)
    tft.text(f"IP:{ip_addr}", 195, 170, WHITE, BLACK)
    
    # 4. Footer Fault Area (Y=200 to 240)
    tft.fill_rect(0, 195, 320, 45, BLACK) # Clear footer
    
    if state == "TRIPPED":
        desc = ", ".join(active_faults) if active_faults else "UNKNOWN_FAULT"
        tft.fill_rect(0, 198, 320, 42, DARKRED)
        tft.text("🚨 FAULT TRIP ACTIVE!", 10, 205, WHITE, DARKRED)
        tft.text(desc[:38], 10, 222, YELLOW, DARKRED)
    elif state == "RESTART_DELAY":
        tft.fill_rect(0, 198, 320, 42, DARKGRAY)
        tft.text("⏳ POWER RETURN WAIT...", 10, 205, YELLOW, DARKGRAY)
        tft.text("Stabilizing voltage lines...", 10, 222, WHITE, DARKGRAY)
    else:
        tft.fill_rect(0, 198, 320, 1, GRAY)
        tft.text("SYSTEM NORMAL", 10, 212, GREEN, BLACK)
        if sim_mode:
            tft.text("[SIMULATION]", 180, 212, YELLOW, BLACK)
        else:
            tft.text("[HW ACTIVE]", 180, 212, CYAN, BLACK)

    # 5. Command Toast Overlay (shows for 3 seconds)
    global last_command, last_command_time
    if last_command is not None and (time.time() - last_command_time < 3):
        tft.fill_rect(0, 195, 320, 45, YELLOW)
        tft.text(f"📥 CMD: {last_command}", 10, 212, BLACK, YELLOW)

def display_thread(heartbeats=None):
    global running
    print("📺 Display status refresh thread started.")
    running = True
    
    # Draw initial dashboard frame
    try:
        draw_dashboard()
    except Exception as e:
        print("🚨 Initial draw_dashboard failed:", e)
        
    while running:
        if heartbeats is not None:
            heartbeats["display"] = time.time()
        try:
            draw_dashboard()
        except Exception as e:
            print("🚨 draw_dashboard failed:", e)
        gc.collect()
        time.sleep(1)
        
    print("📺 Display status refresh thread stopped.")

def start(heartbeats=None):
    if not init_display():
        return False
    _thread.stack_size(8192)
    _thread.start_new_thread(display_thread, (heartbeats,))
    return True

def stop():
    global running
    running = False
