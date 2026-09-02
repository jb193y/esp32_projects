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

# Clean memory diagnostics
gc.collect()
print(" Free Memory:", gc.mem_free(), "bytes")

print(">>> HUB boot.py COMPLETED <<<")

import main
