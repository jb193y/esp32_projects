# boot.py (Hub)
import gc
import sys
import machine
import time

print(">>> HUB boot.py STARTED <<<")
time.sleep(1) # delay for serial connection stabilizer

# Hardware and environment diagnostics
print(" Platform:", sys.platform)
print(" CPU Frequency:", machine.freq() // 1000000, "MHz")
gc.collect()
print(" Free Memory:", gc.mem_free(), "bytes")

print(">>> HUB boot.py COMPLETED <<<")

import main
