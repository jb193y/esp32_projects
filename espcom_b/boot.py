import gc
import sys
import time

print(">>> ESPCOM_B boot.py STARTED <<<")
time.sleep_ms(200)
print(" Platform:", sys.platform)
print(" CPU Frequency:", gc.mem_free())
gc.collect()
print(" Free Memory:", gc.mem_free(), "bytes")
print(">>> ESPCOM_B boot.py COMPLETED <<<")

import main
