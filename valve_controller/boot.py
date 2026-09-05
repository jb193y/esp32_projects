# boot.py (Valve Controller)
import gc
import sys
import machine
import time

print(">>> VALVE boot.py STARTED <<<")
print(" Safe boot delay... press Ctrl-C to enter REPL")
time.sleep(4) # delay for serial connection stabilizer and thread prevention

print(" Platform:", sys.platform)
print(" CPU Frequency:", machine.freq() // 1000000, "MHz")
gc.collect()
print(" Total Memory:", gc.mem_total(), "bytes")
print(" Free Memory:", gc.mem_free(), "bytes")

print(">>> VALVE boot.py COMPLETED <<<")

import main
