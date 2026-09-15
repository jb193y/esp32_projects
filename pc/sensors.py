# sensors.py (Pump Controller)
import machine
import math
import time
import config

_current_adc = None
_voltage_adc = None

def init_sensors():
    global _current_adc, _voltage_adc
    cfg = config.load_config()
    pins = cfg.get("pump", {}).get("pins", {})
    
    current_pin = pins.get("current_sensor", 34)
    voltage_pin = pins.get("voltage_sensor", 35)
    
    # Initialize ADCs (attenuation for full range 0-3.3V)
    _current_adc = machine.ADC(machine.Pin(current_pin))
    _current_adc.atten(machine.ADC.ATTN_11DB)
    
    _voltage_adc = machine.ADC(machine.Pin(voltage_pin))
    _voltage_adc.atten(machine.ADC.ATTN_11DB)
    
    print(f"🔌 Sensors initialized on Pins: Current=GPIO{current_pin}, Voltage=GPIO{voltage_pin}")

def read_rms_current(samples=200, calibration=30.0):
    """
    Reads SCT-013 current sensor.
    Assumes 3.3V logic, 12-bit ADC.
    calibration: Amps per volt of offset output
    """
    global _current_adc
    if _current_adc is None:
        return 0.0
        
    sum_sq = 0
    count = 0
    raw_samples = []
    start_time = time.ticks_ms()
    
    # Sample for 100ms (covering 5 cycles of 50Hz AC or 6 cycles of 60Hz AC)
    while time.ticks_diff(time.ticks_ms(), start_time) < 100 and count < samples:
        val = _current_adc.read()
        raw_samples.append(val)
        count += 1
        time.sleep_us(500)
        
    if count == 0:
        return 0.0
        
    # Calculate midpoint dynamically to remove DC offset
    midpoint = sum(raw_samples) / count
    
    for val in raw_samples:
        diff = val - midpoint
        # Convert ADC difference to voltage (3.3V / 4095)
        voltage_diff = diff * (3.3 / 4095.0)
        sum_sq += voltage_diff * voltage_diff
        
    rms_voltage = math.sqrt(sum_sq / count)
    rms_current = rms_voltage * calibration
    
    # Filter noise threshold
    if rms_current < 0.15:
        rms_current = 0.0
        
    return rms_current

def read_rms_voltage(samples=200, calibration=110.0):
    """
    Reads Grid Voltage sensor.
    calibration: scale factor to convert to actual AC voltage (e.g. 110V or 230V RMS)
    """
    global _voltage_adc
    if _voltage_adc is None:
        return 0.0
        
    sum_sq = 0
    count = 0
    raw_samples = []
    start_time = time.ticks_ms()
    
    # Sample for 100ms
    while time.ticks_diff(time.ticks_ms(), start_time) < 100 and count < samples:
        val = _voltage_adc.read()
        raw_samples.append(val)
        count += 1
        time.sleep_us(500)
        
    if count == 0:
        return 0.0
        
    # Calculate midpoint dynamically to remove DC offset
    midpoint = sum(raw_samples) / count
    
    for val in raw_samples:
        diff = val - midpoint
        voltage_diff = diff * (3.3 / 4095.0)
        sum_sq += voltage_diff * voltage_diff
        
    rms_input_voltage = math.sqrt(sum_sq / count)
    actual_voltage = rms_input_voltage * calibration
    
    if actual_voltage < 10.0:
        actual_voltage = 0.0
        
    return actual_voltage
