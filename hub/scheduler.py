# scheduler.py (Hub)
import time
import _thread
import config

_queue = [] # List of tuples: (node_id, deficit_value, duration_sec)
_lock = _thread.allocate_lock()

# Timers
_last_pump_stop_time = 0
_well_recharge_until = 0
_stabilization_until = 0

def init_scheduler():
    global _stabilization_until
    cfg = config.load_config()
    stab_delay = cfg.get("scheduler", {}).get("load_shedding_stabilization_delay_sec", 300)
    # Set initial stabilization delay on boot
    _stabilization_until = time.time() + stab_delay
    print(f"Scheduler initialized. Power stabilization active until {time.time() + stab_delay} (in {stab_delay}s)")

def queue_irrigation(node_id, deficit, duration_sec):
    global _queue
    with _lock:
        # Check if already in queue; if so, update it
        found = False
        for i, item in enumerate(_queue):
            if item[0] == node_id:
                _queue[i] = (node_id, max(item[1], deficit), duration_sec)
                found = True
                break
        if not found:
            _queue.append((node_id, deficit, duration_sec))
        
        # Sort queue by deficit value descending (highest deficit first)
        _queue.sort(key=lambda x: x[1], reverse=True)
        print(f"Irrigation Queued: {node_id} (Deficit={deficit}, Duration={duration_sec}s). Queue size={len(_queue)}")

def trigger_well_recharge():
    global _well_recharge_until
    cfg = config.load_config()
    delay = cfg.get("scheduler", {}).get("well_recharge_delay_sec", 1800)
    _well_recharge_until = time.time() + delay
    print(f"Well recharge triggered. Pump locked for {delay} seconds.")

def check_pump_allowed():
    now = time.time()
    if now < _stabilization_until:
        remaining = int(_stabilization_until - now)
        print(f"Pump blocked: Grid stabilizing. Remaining: {remaining}s")
        return False, f"stabilizing_{remaining}s"
    if now < _well_recharge_until:
        remaining = int(_well_recharge_until - now)
        print(f"Pump blocked: Well recharging. Remaining: {remaining}s")
        return False, f"recharging_{remaining}s"
    return True, "ready"

def scheduler_thread(heartbeats=None, dispatch_fn=None):
    global _queue, _last_pump_stop_time
    print("Scheduler Thread Started")
    init_scheduler()
    
    active_node = None
    active_end_time = 0
    
    while True:
        if heartbeats is not None:
            heartbeats["scheduler"] = time.time()
            
        now = time.time()
        
        # 1. Process active running zone
        if active_node is not None:
            if now >= active_end_time:
                print(f"Irrigation cycle complete for node: {active_node}")
                # Stop irrigation command to node
                if dispatch_fn:
                    dispatch_fn(active_node, "PUMP_OFF", [])
                
                # Update last stop time for recharge/stabilization logic
                _last_pump_stop_time = now
                trigger_well_recharge() # Enforce well recharge delay after running
                active_node = None
            else:
                # Still running, sleep
                time.sleep(1)
                continue
                
        # 2. Check if we can start a new irrigation cycle
        allowed, status_str = check_pump_allowed()
        if allowed and len(_queue) > 0:
            with _lock:
                node_id, deficit, duration = _queue.pop(0)
                
            print(f"Starting irrigation: {node_id} (Deficit={deficit}, Duration={duration}s)")
            if dispatch_fn:
                # Dispatch PUMP_ON to node
                dispatch_fn(node_id, "PUMP_ON", [], {"duration": duration})
                active_node = node_id
                active_end_time = now + duration
            else:
                print("No dispatch function registered with scheduler")
                
        time.sleep(2)
