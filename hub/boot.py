# boot.py (Hub)
import gc
import sys
import machine
import time

print(">>> HUB boot.py STARTED <<<")
print(" Safe boot delay... press Ctrl-C to enter REPL")
time.sleep(4) # delay for serial connection stabilizer and thread prevention

# Hardware and environment diagnostics
print(" Platform:", sys.platform)
print(" CPU Frequency:", machine.freq() // 1000000, "MHz")

# Clean Wi-Fi subsystem reset to release any stale DMA memory from soft reboots
try:
    import network
    network.WLAN(network.AP_IF).active(False)
    sta = network.WLAN(network.STA_IF)
    sta.active(False)
    time.sleep_ms(50)
    sta.active(True)
    try:
        sta.config(pm=network.WLAN.PM_NONE)
    except:
        pass
    print(" Wi-Fi Radio initialized cleanly")
except Exception as wifi_boot_err:
    print(" Wi-Fi boot init notice:", wifi_boot_err)

print("Delaying for stability...")
time.sleep(4)
gc.collect()
alloc = gc.mem_alloc()
free = gc.mem_free()
print(" Total Memory:", alloc + free, "bytes")
print(" Free Memory:", free, "bytes")

print(">>> HUB boot.py COMPLETED <<<")

import main
