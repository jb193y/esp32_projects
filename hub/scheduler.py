# scheduler.py (Hub)
import time
import _thread
import ujson
import os
import config

_queue = [] # List of tuples: (node_id, deficit_value, duration_sec, valve_id)
_active_runs = {} # dict mapping node_id -> {end_time, allow_parallel, duration, valve_id}
_active_surpluses = {} # dict mapping resource_name -> expiration_unix_time
_lock = _thread.allocate_lock()

# Timers
_last_pump_stop_time = 0
_well_recharge_until = 0
_stabilization_until = 0

# Broadcast Callback for MQTT sync
_broadcast_schedules_cb = None

SCHEDULES_FILE = "schedules.json"
STATUS_FILE = "scheduler_status.json"

def register_broadcast_callback(cb):
    global _broadcast_schedules_cb
    _broadcast_schedules_cb = cb

def broadcast_schedules_state():
    if _broadcast_schedules_cb:
        _broadcast_schedules_cb()

def load_schedules():
    try:
        with open(SCHEDULES_FILE, "r") as f:
            data = ujson.load(f)
            return data.get("schedules", [])
    except:
        return []

def save_schedules(schedules):
    try:
        with open(SCHEDULES_FILE, "w") as f:
            ujson.dump({"schedules": schedules}, f)
    except Exception as e:
        print("Failed to save schedules:", e)

def save_scheduler_status():
    global _queue, _well_recharge_until, _stabilization_until, _active_runs, _active_surpluses
    try:
        now = config.get_unix_time()
        # Persist timestamps as offsets relative to now to survive reboot without RTC sync
        status_data = {
            "well_recharge_offset": max(0, int(_well_recharge_until - now)),
            "stabilization_offset": max(0, int(_stabilization_until - now)),
            "queue": [
                {"node_id": item[0], "deficit": item[1], "duration": item[2], "valve_id": item[3] if len(item) > 3 else "1"}
                for item in _queue
            ],
            "active_runs": {
                node_id: {
                    "end_time_offset": max(0, int(info["end_time"] - now)),
                    "allow_parallel": info["allow_parallel"],
                    "duration": info["duration"],
                    "valve_id": info.get("valve_id", "1")
                }
                for node_id, info in _active_runs.items()
            },
            "active_surpluses": {
                res_name: max(0, int(expire_time - now))
                for res_name, expire_time in _active_surpluses.items()
            }
        }
        with open(STATUS_FILE, "w") as f:
            ujson.dump(status_data, f)
    except Exception as e:
        print("Failed to save scheduler status:", e)

def load_scheduler_status():
    global _queue, _well_recharge_until, _stabilization_until, _active_runs, _active_surpluses
    try:
        with open(STATUS_FILE, "r") as f:
            status_data = ujson.load(f)
            
        now = config.get_unix_time()
        
        recharge_offset = status_data.get("well_recharge_offset", 0)
        _well_recharge_until = now + recharge_offset
        
        stabilization_offset = status_data.get("stabilization_offset", 0)
        _stabilization_until = now + stabilization_offset
        
        _queue = []
        for item in status_data.get("queue", []):
            _queue.append((item["node_id"], item["deficit"], item["duration"], item.get("valve_id", "1")))
            
        _active_runs = {}
        for node_id, info in status_data.get("active_runs", {}).items():
            _active_runs[node_id] = {
                "end_time": now + info["end_time_offset"],
                "allow_parallel": info["allow_parallel"],
                "duration": info["duration"],
                "valve_id": info.get("valve_id", "1")
            }
            
        _active_surpluses = {}
        for res_name, offset in status_data.get("active_surpluses", {}).items():
            _active_surpluses[res_name] = now + offset
            
        print(f"Scheduler status loaded: {len(_queue)} queued, {len(_active_runs)} active runs, {len(_active_surpluses)} active surpluses.")
    except Exception as e:
        print("No scheduler status found or failed to load. Initializing clean scheduler state.")
        init_scheduler()

def init_scheduler():
    global _stabilization_until
    cfg = config.load_config()
    stab_delay = cfg.get("scheduler", {}).get("load_shedding_stabilization_delay_sec", 300)
    _stabilization_until = config.get_unix_time() + stab_delay
    print(f"Scheduler initialized. Power stabilization active until {config.get_unix_time() + stab_delay} (in {stab_delay}s)")

def queue_irrigation(node_id, deficit, duration_sec, valve_id="1"):
    global _queue
    with _lock:
        found = False
        for i, item in enumerate(_queue):
            if item[0] == node_id and (len(item) > 3 and item[3] == valve_id):
                _queue[i] = (node_id, max(item[1], deficit), duration_sec, valve_id)
                found = True
                break
        if not found:
            _queue.append((node_id, deficit, duration_sec, valve_id))
        
        _queue.sort(key=lambda x: x[1], reverse=True)
        print(f"Irrigation Queued: {node_id} Port {valve_id} (Deficit={deficit}, Duration={duration_sec}s)")
        save_scheduler_status()

def trigger_well_recharge():
    global _well_recharge_until
    cfg = config.load_config()
    delay = cfg.get("scheduler", {}).get("well_recharge_delay_sec", 1800)
    _well_recharge_until = config.get_unix_time() + delay
    print(f"Well recharge triggered. Pump locked for {delay} seconds.")
    save_scheduler_status()

def set_resource_surplus(resource_name, duration_sec):
    global _active_surpluses
    now = config.get_unix_time()
    if duration_sec > 0:
        _active_surpluses[resource_name] = now + duration_sec
        print(f"☀️ Resource surplus activated: {resource_name} for {duration_sec}s (until {now + duration_sec})")
    else:
        _active_surpluses.pop(resource_name, None)
        print(f"☀️ Resource surplus cleared for {resource_name}")
    save_scheduler_status()

def is_surplus_active():
    global _active_surpluses
    now = config.get_unix_time()
    active = False
    for res_name, expire_time in list(_active_surpluses.items()):
        if now < expire_time:
            active = True
        else:
            _active_surpluses.pop(res_name, None)
    return active

def check_pump_allowed():
    now = config.get_unix_time()
    if now < _stabilization_until:
        remaining = int(_stabilization_until - now)
        return False, f"stabilizing_{remaining}s"
    if now < _well_recharge_until:
        remaining = int(_well_recharge_until - now)
        return False, f"recharging_{remaining}s"
    return True, "ready"

def parse_datetime_to_epoch(date_str, time_str):
    try:
        date_str = str(date_str)
        if '-' in date_str:
            parts = date_str.split('-')
        else:
            parts = date_str.split('/')
        year = int(parts[0])
        month = int(parts[1])
        day = int(parts[2])
        
        t_parts = str(time_str).split(':')
        hour = int(t_parts[0])
        minute = int(t_parts[1])
        
        epoch = time.mktime((year, month, day, hour, minute, 0, 0, 0))
        return epoch + 946684800
    except Exception as e:
        # Fallback to current year/month/day
        try:
            now_local = time.localtime()
            t_parts = str(time_str).split(':')
            hour = int(t_parts[0])
            minute = int(t_parts[1])
            epoch = time.mktime((now_local[0], now_local[1], now_local[2], hour, minute, 0, 0, 0))
            return epoch + 946684800
        except:
            return 0

def calculate_next_run(schedule):
    freq = schedule.get("frequency_days", 1)
    if freq < 1:
        freq = 1
    step = freq * 86400
        
    start_epoch = parse_datetime_to_epoch(schedule.get("start_date", "2026-01-01"), schedule.get("start_time", "00:00"))
    if start_epoch == 0:
        return 0
        
    now = config.get_unix_time()
    last_run = schedule.get("last_run_time", 0)
    lead_window = schedule.get("lead_window_sec", 0)
    
    # Start at the configured start date and time
    next_time = start_epoch
    
    # Align next_time to the slot sequence: start_epoch + k * step
    if now > start_epoch:
        diff = now - start_epoch
        k = diff // step
        next_time = start_epoch + k * step
        
    # Ensure next_time is strictly after last_run
    while next_time <= last_run:
        next_time += step
        
    # Early-run safeguard: if last_run is close to next_time (within the lead_window_sec),
    # it means this slot has already been run early. Advance to the next slot.
    if last_run > 0 and (next_time - last_run) <= lead_window:
        next_time += step
        
    return next_time

def check_and_trigger_schedules():
    schedules = load_schedules()
    now = config.get_unix_time()
    changed = False
    
    surplus_active = is_surplus_active()
    
    for s in schedules:
        if not s.get("enabled", True):
            continue
            
        next_run = s.get("next_run_time", 0)
        if next_run == 0 or (next_run <= now and s.get("last_run_time", 0) >= next_run):
            next_run = calculate_next_run(s)
            s["next_run_time"] = next_run
            changed = True
            
        if next_run > 0:
            lead_window = s.get("lead_window_sec", 0)
            triggered = False
            
            # Normal trigger
            if now >= next_run:
                triggered = True
                print(f"⏰ Schedule {s['schedule_id']} triggered for {s['node_id']} Port {s.get('valve_id', '1')}!")
            # Lead window early start trigger
            elif surplus_active and lead_window > 0 and (now + lead_window >= next_run):
                triggered = True
                print(f"☀️ Schedule {s['schedule_id']} triggered early via lead window ({lead_window}s, surplus active) for {s['node_id']} Port {s.get('valve_id', '1')}!")
                
            if triggered:
                queue_irrigation(s["node_id"], 100.0, s["duration_sec"], s.get("valve_id", "1"))
                s["last_run_time"] = now
                s["next_run_time"] = calculate_next_run(s)
                changed = True
                
    if changed:
        save_schedules(schedules)
        broadcast_schedules_state()

def add_or_update_schedule(sched_dict):
    schedules = load_schedules()
    sched_id = sched_dict.get("schedule_id")
    if not sched_id:
        return False, "Missing schedule_id"
    if not sched_dict.get("node_id"):
        return False, "Missing node_id"
        
    found = False
    for i, s in enumerate(schedules):
        if s.get("schedule_id") == sched_id:
            s.update(sched_dict)
            s["next_run_time"] = calculate_next_run(s)
            found = True
            break
            
    if not found:
        sched_dict["next_run_time"] = calculate_next_run(sched_dict)
        sched_dict.setdefault("last_run_time", 0)
        sched_dict.setdefault("allow_parallel", False)
        sched_dict.setdefault("lead_window_sec", 0)
        sched_dict.setdefault("valve_id", "1")
        sched_dict.setdefault("enabled", True)
        schedules.append(sched_dict)
        
    save_schedules(schedules)
    broadcast_schedules_state()
    return True, "Schedule updated successfully"

def remove_schedule(sched_id):
    schedules = load_schedules()
    initial_len = len(schedules)
    schedules = [s for s in schedules if s.get("schedule_id") != sched_id]
    if len(schedules) == initial_len:
        return False, "Schedule not found"
        
    save_schedules(schedules)
    broadcast_schedules_state()
    return True, "Schedule deleted successfully"

def scheduler_thread(heartbeats=None, dispatch_fn=None):
    global _queue, _last_pump_stop_time, _well_recharge_until, _stabilization_until, _active_runs
    print("Scheduler Thread Started")
    
    load_scheduler_status()
    
    if dispatch_fn:
        now = config.get_unix_time()
        for node_id, info in _active_runs.items():
            rem_dur = int(info["end_time"] - now)
            if rem_dur > 0:
                print(f"Restoring active run for {node_id} Port {info['valve_id']} ({rem_dur}s remaining)")
                dispatch_fn(node_id, "PUMP_ON", [], {"valve_id": info["valve_id"], "duration": rem_dur})
            
    last_schedule_check = 0
    
    while True:
        if heartbeats is not None:
            heartbeats["scheduler"] = time.time()
            
        now = config.get_unix_time()
        
        runs_changed = False
        for node_id, info in list(_active_runs.items()):
            if now >= info["end_time"]:
                print(f"Irrigation cycle complete for node: {node_id} Port {info['valve_id']}")
                if dispatch_fn:
                    dispatch_fn(node_id, "PUMP_OFF", [], {"valve_id": info["valve_id"]})
                _active_runs.pop(node_id)
                runs_changed = True
                
                if not info["allow_parallel"]:
                    _last_pump_stop_time = now
                    trigger_well_recharge()
        
        if runs_changed:
            save_scheduler_status()
            
        if now - last_schedule_check >= 30:
            last_schedule_check = now
            check_and_trigger_schedules()
            
        if len(_queue) > 0:
            with _lock:
                next_item = _queue[0]
            
            node_id, deficit, duration, valve_id = next_item
            
            schedules = load_schedules()
            allow_parallel = False
            for s in schedules:
                if s.get("node_id") == node_id and str(s.get("valve_id", "1")) == str(valve_id):
                    allow_parallel = s.get("allow_parallel", False)
                    break
            
            can_start = False
            has_sequential_run = any(not r["allow_parallel"] for r in _active_runs.values())
            pump_allowed, status_str = check_pump_allowed()
            
            if pump_allowed:
                if allow_parallel:
                    if not has_sequential_run:
                        can_start = True
                else:
                    if not _active_runs:
                        can_start = True
            
            if can_start:
                with _lock:
                    _queue.pop(0)
                
                print(f"Starting irrigation cycle: {node_id} Port {valve_id} (Deficit={deficit}, Duration={duration}s, Parallel={allow_parallel})")
                if dispatch_fn:
                    dispatch_fn(node_id, "PUMP_ON", [], {"valve_id": valve_id, "duration": duration})
                    _active_runs[node_id] = {
                        "end_time": now + duration,
                        "allow_parallel": allow_parallel,
                        "duration": duration,
                        "valve_id": valve_id
                    }
                    save_scheduler_status()
                else:
                    print("No dispatch function registered with scheduler")
                    
        time.sleep(2)
