# boot.py (Valve Controller)
import gc
import sys
import machine
import time

print(">>> VALVE boot.py STARTED <<<")
time.sleep(1) # delay for serial connection stabilizer

print("💻 Platform:", sys.platform)
print("🕒 CPU Frequency:", machine.freq() // 1000000, "MHz")
gc.collect()
print("🗄️ Free Memory:", gc.mem_free(), "bytes")

print(">>> VALVE boot.py COMPLETED <<<")
