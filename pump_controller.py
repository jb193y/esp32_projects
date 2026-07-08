# pump_controller.py
import time
import machine
import math
import ujson
import _thread
import os
import gc
import config

# --- Global State and Thread Lock ---
lock = _thread.allocate_lock()
state = "OFF"                  # OFF, STARTING, RUNNING, TRIPPED, RESTART_DELAY
active_faults = []             # List of active fault strings
telemetry = {}                 # Latest sensor readings
last_start_time = 0
start_count_hour = 0
start_timestamps = []          # List of start epoch timestamps
last_operator = ""             # Stores username of last pump operation

# Statistics
runtime_sec = 0
daily_runtime_sec = 0
est_kwh = 0.0
last_stat_update = 0

# IO References
pins = {}
adcs = {}
solenoid_pins = []

# Simulation toggle
SIMULATE = True
sim_voltage_err = None
sim_current_err = None
sim_estop = False
sim_flow = True
sim_tank_level = "MID"

def init_hardware():
    global pins, adcs, solenoid_pins, SIMULATE
    cfg = config.load_config()
    pump_cfg = cfg.get("pump", {})
    pins_cfg = pump_cfg.get("pins", {})
    
    SIMULATE = pump_cfg.get("simulation_mode", True)
    print("🔌 Pump Hardware Init. Simulation Mode:", SIMULATE)
    
    if SIMULATE:
        return
        
    try:
        # Outputs
        pins["relay_contactor"] = machine.Pin(pins_cfg.get("relay_contactor", 12), machine.Pin.OUT)
        pins["relay_contactor"].value(0)
        
        pins["buzzer"] = machine.Pin(pins_cfg.get("buzzer", 21), machine.Pin.OUT)
        pins["buzzer"].value(0)
        
        pins["led_run"] = machine.Pin(pins_cfg.get("led_run", 2), machine.Pin.OUT)
        pins["led_run"].value(0)
        
        pins["led_fault"] = machine.Pin(pins_cfg.get("led_fault", 22), machine.Pin.OUT)
        pins["led_fault"].value(0)
        
        solenoid_pins = []
        for p in pins_cfg.get("solenoids", [25, 26]):
            pin_obj = machine.Pin(p, machine.Pin.OUT)
            pin_obj.value(0)
            solenoid_pins.append(pin_obj)
            
        # Inputs (Opto-isolated, usually active low or high depending on wiring, we assume active high with pull-down)
        pins["contactor_feedback"] = machine.Pin(pins_cfg.get("contactor_feedback", 13), machine.Pin.IN, machine.Pin.PULL_DOWN)
        pins["estop"] = machine.Pin(pins_cfg.get("estop", 14), machine.Pin.IN, machine.Pin.PULL_UP) # NC switch usually pull-up
        pins["tank_high"] = machine.Pin(pins_cfg.get("tank_high", 15), machine.Pin.IN, machine.Pin.PULL_DOWN)
        pins["tank_low"] = machine.Pin(pins_cfg.get("tank_low", 16), machine.Pin.IN, machine.Pin.PULL_DOWN)
        pins["flow_sensor"] = machine.Pin(pins_cfg.get("flow_sensor", 17), machine.Pin.IN, machine.Pin.PULL_DOWN)
        pins["btn_start"] = machine.Pin(pins_cfg.get("btn_start", 18), machine.Pin.IN, machine.Pin.PULL_UP)
        pins["btn_stop"] = machine.Pin(pins_cfg.get("btn_stop", 19), machine.Pin.IN, machine.Pin.PULL_UP)
        pins["btn_setup"] = machine.Pin(pins_cfg.get("btn_setup", 0), machine.Pin.IN, machine.Pin.PULL_UP)
        
        # ADCs
        adcs["v_a"] = machine.ADC(machine.Pin(pins_cfg.get("adc_v_a", 1)))
        adcs["v_b"] = machine.ADC(machine.Pin(pins_cfg.get("adc_v_b", 2)))
        adcs["v_c"] = machine.ADC(machine.Pin(pins_cfg.get("adc_v_c", 3)))
        adcs["i_a"] = machine.ADC(machine.Pin(pins_cfg.get("adc_i_a", 4)))
        adcs["i_b"] = machine.ADC(machine.Pin(pins_cfg.get("adc_i_b", 5)))
        adcs["i_c"] = machine.ADC(machine.Pin(pins_cfg.get("adc_i_c", 6)))
        adcs["pressure"] = machine.ADC(machine.Pin(pins_cfg.get("pressure_sensor", 8)))
        
        # Attenuate for full 3.3V range
        for adc in adcs.values():
            adc.atten(machine.ADC.ATTN_11DB)
            
    except Exception as e:
        print("🚨 Hardware initialization error, falling back to Simulation mode:", e)
        SIMULATE = True

# --- RMS Sensing Calculations ---
def sample_adc_rms(adc, factor, offset=2048, samples=50):
    if SIMULATE:
        return 0.0
    try:
        sum_sq = 0
        for _ in range(samples):
            val = adc.read() - offset
            sum_sq += val * val
            time.sleep_us(100)
        mean_sq = sum_sq / samples
        return math.sqrt(mean_sq) * factor
    except:
        return 0.0

def read_sensors():
    global telemetry
    cfg = config.load_config()
    pump_cfg = cfg.get("pump", {})
    cal = pump_cfg.get("calibration", {})
    
    if SIMULATE:
        # Simulate voltage around 230V RMS +/- noise
        v_noise = (time.ticks_ms() % 7) - 3
        v_a = 230.0 + v_noise if sim_voltage_err != "phase_loss_a" else 10.0
        v_b = 228.0 + v_noise if sim_voltage_err != "phase_loss_b" else 10.0
        v_c = 231.0 + v_noise if sim_voltage_err != "phase_loss_c" else 10.0
        
        if sim_voltage_err == "over_voltage":
            v_a, v_b, v_c = 275.0, 273.0, 274.0
        elif sim_voltage_err == "under_voltage":
            v_a, v_b, v_c = 160.0, 162.0, 161.0
            
        # Simulate current depending on contactor state
        is_on = (state in ["STARTING", "RUNNING"])
        if is_on:
            i_noise = (time.ticks_ms() % 5) / 10.0 - 0.25
            i_a = 12.0 + i_noise
            i_b = 11.8 + i_noise
            i_c = 12.1 + i_noise
            
            if sim_current_err == "overload":
                i_a, i_b, i_c = 22.0, 21.5, 23.0
            elif sim_current_err == "dry_run":
                i_a, i_b, i_c = 2.5, 2.4, 2.6
            elif sim_current_err == "unbalance":
                i_a, i_b, i_c = 12.0, 6.0, 12.0
            elif sim_current_err == "locked_rotor":
                i_a, i_b, i_c = 45.0, 44.5, 46.0
        else:
            i_a, i_b, i_c = 0.0, 0.0, 0.0
            
        pressure = 45.0 if is_on else 5.0
        flow_rate = 18.5 if (is_on and sim_flow) else 0.0
        estop_active = sim_estop
        fb_active = is_on  # contactor follows relay command in simulation
        
        tank_low = (sim_tank_level == "LOW")
        tank_high = (sim_tank_level == "HIGH")
    else:
        # Read hardware
        v_a = sample_adc_rms(adcs["v_a"], cal.get("v_a_factor", 1.0))
        v_b = sample_adc_rms(adcs["v_b"], cal.get("v_b_factor", 1.0))
        v_c = sample_adc_rms(adcs["v_c"], cal.get("v_c_factor", 1.0))
        
        i_a = sample_adc_rms(adcs["i_a"], cal.get("i_a_factor", 1.0))
        i_b = sample_adc_rms(adcs["i_b"], cal.get("i_b_factor", 1.0))
        i_c = sample_adc_rms(adcs["i_c"], cal.get("i_c_factor", 1.0))
        
        pressure_raw = adcs["pressure"].read()
        pressure = (pressure_raw / 4095.0) * 100.0 # simple 0-100 psi scaling
        
        # Simple digital/pulse read for flow (could be counter in interrupt, simplified here)
        flow_rate = 15.0 if pins["flow_sensor"].value() == 1 else 0.0
        
        estop_active = (pins["estop"].value() == 0) # Active low
        fb_active = (pins["contactor_feedback"].value() == 1)
        tank_low = (pins["tank_low"].value() == 1)
        tank_high = (pins["tank_high"].value() == 1)
        
    lock.acquire()
    try:
        telemetry = {
            "v_a": round(v_a, 1), "v_b": round(v_b, 1), "v_c": round(v_c, 1),
            "i_a": round(i_a, 2), "i_b": round(i_b, 2), "i_c": round(i_c, 2),
            "v_avg": round((v_a + v_b + v_c)/3.0, 1),
            "i_avg": round((i_a + i_b + i_c)/3.0, 2),
            "pressure": round(pressure, 1),
            "flow_rate": round(flow_rate, 1),
            "estop": estop_active,
            "feedback": fb_active,
            "tank_low": tank_low,
            "tank_high": tank_high,
            "tank_level_str": "HIGH" if tank_high else "LOW" if tank_low else "MID",
            "mode": pump_cfg.get("mode", "MANUAL"),
            "maintenance_locked_by": pump_cfg.get("maintenance_locked_by", "")
        }
    finally:
        lock.release()

# --- Logging helpers ---
def log_event(event_type, details):
    t = time.time()
    log_entry = ujson.dumps({"t": t, "e": event_type, "d": details})
    print("📝 EVENT:", event_type, "-", details)
    try:
        with open("events.jsonl", "a") as f:
            f.write(log_entry + "\n")
    except Exception as e:
        print("❌ Log event failed:", e)

def log_fault(fault_type, desc):
    t = time.time()
    read_sensors()
    f_entry = ujson.dumps({
        "t": t, "f": fault_type, "desc": desc,
        "v": [telemetry["v_a"], telemetry["v_b"], telemetry["v_c"]],
        "i": [telemetry["i_a"], telemetry["i_b"], telemetry["i_c"]]
    })
    print("🚨 FAULT LOGGED:", fault_type, desc)
    try:
        with open("faults.jsonl", "a") as f:
            f.write(f_entry + "\n")
    except Exception as e:
        print("❌ Log fault failed:", e)

# --- State Machine Transitions ---
def set_state(new_state, reason="", operator=None):
    global state, last_start_time
    if state == new_state:
        return
    
    print(f"🔄 State change: {state} ➔ {new_state} (Reason: {reason})")
    op_str = f" by {operator}" if operator else ""
    log_event("STATE_CHANGE", f"{state} to {new_state} due to {reason}{op_str}")
    
    state = new_state
    
    if not SIMULATE:
        try:
            if state in ["STARTING", "RUNNING"]:
                pins["relay_contactor"].value(1)
                pins["led_run"].value(1)
                pins["led_fault"].value(0)
            elif state == "TRIPPED":
                pins["relay_contactor"].value(0)
                pins["led_run"].value(0)
                pins["led_fault"].value(1)
                pins["buzzer"].value(1)
            else:
                pins["relay_contactor"].value(0)
                pins["led_run"].value(0)
                pins["led_fault"].value(0)
                pins["buzzer"].value(0)
        except Exception as e:
            print("🚨 Pin control failure:", e)

# --- Protection Checker ---
def check_protections():
    global active_faults, start_timestamps, start_count_hour
    cfg = config.load_config()
    p_cfg = cfg.get("pump", {})
    limits = p_cfg.get("thresholds", {})
    
    read_sensors()
    faults = []
    
    # 1. Emergency Stop
    if telemetry["estop"]:
        faults.append("EMERGENCY_STOP")
        
    # 2. Voltage Protections (Phase Failure, Over, Under, Imbalance)
    v_a, v_b, v_c = telemetry["v_a"], telemetry["v_b"], telemetry["v_c"]
    v_avg = telemetry["v_avg"]
    
    if v_a < 50.0 or v_b < 50.0 or v_c < 50.0:
        faults.append("PHASE_FAILURE")
    else:
        if v_avg > limits.get("v_over", 260.0):
            faults.append("OVER_VOLTAGE")
        elif v_avg < limits.get("v_under", 180.0):
            faults.append("UNDER_VOLTAGE")
            
        # Voltage Imbalance Calculation
        v_devs = [abs(v_a - v_avg), abs(v_b - v_avg), abs(v_c - v_avg)]
        max_v_dev = max(v_devs)
        v_imbalance = (max_v_dev / v_avg) * 100.0 if v_avg > 0 else 0.0
        if v_imbalance > limits.get("v_unbalance_pct", 10.0):
            faults.append("VOLTAGE_IMBALANCE")
            
    # 3. Current Protections (Overload, Dry Run, Imbalance, Jam)
    if state in ["STARTING", "RUNNING"]:
        i_a, i_b, i_c = telemetry["i_a"], telemetry["i_b"], telemetry["i_c"]
        i_avg = telemetry["i_avg"]
        
        # Current unbalance (only check if motor drawing significant current)
        if i_avg > 2.0:
            i_devs = [abs(i_a - i_avg), abs(i_b - i_avg), abs(i_c - i_avg)]
            max_i_dev = max(i_devs)
            i_imbalance = (max_i_dev / i_avg) * 100.0
            if i_imbalance > limits.get("i_unbalance_pct", 20.0):
                faults.append("CURRENT_IMBALANCE")
                
        # Contactor feedback check
        if not telemetry["feedback"]:
            faults.append("CONTACTOR_FEEDBACK_FAULT")
            
    active_faults = faults
    return len(faults) > 0

# --- Command Handler ---
def pump_command(cmd, val=None, operator=None):
    global sim_voltage_err, sim_current_err, sim_estop, sim_flow, sim_tank_level, last_operator
    last_operator = operator or ""
    print(f"📥 Pump Command Received: {cmd} = {val} (Operator: {operator})")
    
    cfg = config.load_config()
    
    if cmd == "PUMP_ON":
        if state == "TRIPPED":
            print("⚠️ Cannot turn ON, controller is TRIPPED!")
            return False
        # Check frequent start limit
        now = time.time()
        min_start_interval = cfg.get("pump", {}).get("thresholds", {}).get("frequent_start_min_interval_sec", 300)
        # filter timestamps older than 1 hour
        start_timestamps[:] = [t for t in start_timestamps if now - t < 3600]
        
        if len(start_timestamps) >= 3:
            print("🚨 Blocked: Frequent start limit reached (Max 3 starts per hour)!")
            log_event("START_BLOCKED", "Frequent start limit reached")
            return False
            
        if len(start_timestamps) > 0 and (now - start_timestamps[-1]) < min_start_interval:
            print(f"🚨 Blocked: Must wait {min_start_interval} seconds between starts!")
            log_event("START_BLOCKED", "Minimum restart interval active")
            return False
            
        start_timestamps.append(now)
        set_state("STARTING", "Command ON", operator=operator)
        return True
        
    elif cmd == "PUMP_OFF":
        set_state("OFF", "Command OFF", operator=operator)
        return True
        
    elif cmd == "SET_MODE":
        if val in ["MANUAL", "AUTO", "MAINTENANCE", "SCHEDULED", "SCHEDULE"]:
            val_to_save = "SCHEDULED" if val in ["SCHEDULE", "SCHEDULED"] else val
            curr_mode = cfg.get("pump", {}).get("mode", "MANUAL")
            
            # Unlock logic check
            if curr_mode == "MAINTENANCE" and val_to_save != "MAINTENANCE":
                locked_by = cfg.get("pump", {}).get("maintenance_locked_by", "")
                if locked_by and locked_by != "":
                    if operator and operator != locked_by:
                        print(f"🚨 Unlock rejected: Locked by {locked_by}, attempted by {operator}")
                        return False
                cfg.setdefault("pump", {})["maintenance_locked_by"] = ""
            
            # Lock logic
            if val_to_save == "MAINTENANCE":
                cfg.setdefault("pump", {})["maintenance_locked_by"] = operator or "Unknown User"
                
            cfg.setdefault("pump", {})["mode"] = val_to_save
            config.save_config(cfg)
            log_event("MODE_CHANGE", f"{val_to_save} by {operator or 'System'}")
            if val_to_save == "MAINTENANCE":
                set_state("OFF", "Maintenance mode active", operator=operator)
            return True
        
    elif cmd == "CLEAR_FAULT":
        if state == "TRIPPED":
            print("🧹 Clearing active faults.")
            set_state("OFF", "Fault reset", operator=operator)
            return True
            
    # Simulation Commands for verification
    elif cmd == "SIM_VOLTAGE":
        sim_voltage_err = val
        print(f"🔬 Simulating voltage state: {val}")
    elif cmd == "SIM_CURRENT":
        sim_current_err = val
        print(f"🔬 Simulating current state: {val}")
    elif cmd == "SIM_ESTOP":
        sim_estop = bool(val)
        print(f"🔬 Simulating ESTOP: {val}")
    elif cmd == "SIM_FLOW":
        sim_flow = bool(val)
        print(f"🔬 Simulating FLOW: {val}")
    elif cmd == "SIM_TANK":
        sim_tank_level = val
        print(f"🔬 Simulating TANK: {val}")
        
    return False

# --- Core Thread Loop ---
def pump_thread(heartbeats=None):
    global runtime_sec, daily_runtime_sec, est_kwh, last_stat_update
    _thread.stack_size(8192)
    
    init_hardware()
    last_stat_update = time.time()
    
    # Load settings
    cfg = config.load_config()
    restart_delay = cfg.get("pump", {}).get("thresholds", {}).get("power_return_delay_sec", 15)
    
    # Start up in RESTART_DELAY if power just restored
    print(f"⏳ Power return delay active: waiting {restart_delay} seconds...")
    set_state("RESTART_DELAY", "Power return delay")
    time.sleep(restart_delay)
    set_state("OFF", "Power stabilized")
    
    # Trip checks timers
    overload_duration = 0
    dryrun_duration = 0
    flow_duration = 0
    setup_press_duration = 0
    
    while True:
        try:
            if heartbeats:
                heartbeats["pump"] = time.time()
                
            cfg = config.load_config()
            pump_cfg = cfg.get("pump", {})
            limits = pump_cfg.get("thresholds", {})
            mode = pump_cfg.get("mode", "MANUAL")
            
            # 1. Check protection limits
            has_fault = check_protections()
            
            if has_fault:
                desc = ", ".join(active_faults)
                log_fault("TRIP", desc)
                set_state("TRIPPED", desc)
                
            # 2. Local physical buttons logic (Active low buttons, read raw values)
            if not SIMULATE:
                if pins["btn_stop"].value() == 0:
                    pump_command("PUMP_OFF")
                elif pins["btn_start"].value() == 0 and mode != "MAINTENANCE":
                    pump_command("PUMP_ON")
                
                # Check setup/pairing button (active-low, BOOT pin)
                if pins["btn_setup"].value() == 0:
                    setup_press_duration += 1
                    print(f"⏳ Setup button held: {setup_press_duration}s...")
                    if setup_press_duration >= 5:
                        print("🧹 Entering pairing mode via setup button press! Saving config and resetting...")
                        # Turn off pump immediately for safety
                        pump_command("PUMP_OFF")
                        # Update config mode to ap
                        cfg_to_update = config.load_config()
                        cfg_to_update.setdefault("client", {})["mode"] = "ap"
                        config.save_config(cfg_to_update)
                        # Sound buzzer briefly to signal success
                        try:
                            pins["buzzer"].value(1)
                            time.sleep(0.5)
                            pins["buzzer"].value(0)
                        except: pass
                        machine.reset()
                else:
                    setup_press_duration = 0
                    
            # 3. State Machine Processing
            now = time.time()
            dt = now - last_stat_update
            last_stat_update = now
            
            if state == "STARTING":
                # Check for start timeout / locked rotor
                i_avg = telemetry["i_avg"]
                if i_avg > limits.get("i_overload", 15.0) * 2.5:
                    log_fault("LOCKED_ROTOR", "High startup current locked rotor")
                    set_state("TRIPPED", "LOCKED_ROTOR")
                else:
                    # After 3 seconds, transition to running
                    if now - start_timestamps[-1] >= 3:
                        set_state("RUNNING", "Startup complete")
                        overload_duration = 0
                        dryrun_duration = 0
                        flow_duration = 0
                        
            elif state == "RUNNING":
                # Check Overload duration
                if telemetry["i_avg"] > limits.get("i_overload", 15.0):
                    overload_duration += dt
                    if overload_duration >= limits.get("overload_trip_time_sec", 5):
                        log_fault("OVERLOAD", f"Current {telemetry['i_avg']}A exceeded limit")
                        set_state("TRIPPED", "OVERLOAD")
                else:
                    overload_duration = 0
                    
                # Check Dry Run (Underload current)
                if telemetry["i_avg"] < limits.get("i_dry_run", 4.0):
                    dryrun_duration += dt
                    if dryrun_duration >= limits.get("dry_run_trip_time_sec", 10):
                        log_fault("DRY_RUN", f"Current {telemetry['i_avg']}A below dry limit")
                        set_state("TRIPPED", "DRY_RUN")
                else:
                    dryrun_duration = 0
                    
                # Check Flow Rate (No flow timeout)
                if telemetry["flow_rate"] < 1.0:
                    flow_duration += dt
                    if flow_duration >= limits.get("no_flow_timeout_sec", 15):
                        log_fault("NO_FLOW", "No water flow detected while pump running")
                        set_state("TRIPPED", "NO_FLOW")
                else:
                    flow_duration = 0
                    
                # Statistics update
                runtime_sec += dt
                daily_runtime_sec += dt
                # kWh estimation: P = V * I * PF * sqrt(3) / 1000
                power_kw = (telemetry["v_avg"] * telemetry["i_avg"] * 0.85 * math.sqrt(3)) / 1000.0
                est_kwh += power_kw * (dt / 3600.0)
                
            elif state == "OFF":
                # If AUTO mode, check tank levels
                if mode == "AUTO":
                    if telemetry["tank_low"]:
                        pump_command("PUMP_ON")
                        
            elif state == "TRIPPED":
                # Flash fault LED & beep buzzer (simple block in loop or simulated)
                if not SIMULATE:
                    try:
                        pins["led_fault"].value(not pins["led_fault"].value())
                        # Sound buzzer on/off
                        pins["buzzer"].value(pins["led_fault"].value())
                    except: pass
                    
            # Auto stop on Tank Full (works in both AUTO and MANUAL modes for safety)
            if state in ["STARTING", "RUNNING"] and telemetry["tank_high"]:
                print("💧 Water Tank FULL! Auto shutting down pump.")
                pump_command("PUMP_OFF")
                
            # Keep solenoid zones active according to logic (simplified toggle here)
            if not SIMULATE and len(solenoid_pins) > 0:
                is_on = (state in ["STARTING", "RUNNING"])
                solenoid_pins[0].value(1 if is_on else 0) # Zone 1 follows contactor
                
        except Exception as e:
            print("🚨 Pump thread error:", e)
            
        time.sleep(1)
