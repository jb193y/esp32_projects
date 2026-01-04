# imu.py
from machine import I2C, Pin
from mpu6050 import MPU6050
import time

# -----------------------------
# I2C SETUP
# -----------------------------
i2c = I2C(0, scl=Pin(22), sda=Pin(21), freq=100000)
time.sleep(0.3)

imu = MPU6050(i2c, addr=0x68)

# -----------------------------
# CALIBRATION
# -----------------------------
CALIBRATION_SAMPLES = 200

accel_offset = [0.0, 0.0, 0.0]
gyro_offset = [0.0, 0.0, 0.0]

print("🧭 Calibrating IMU... Keep device STILL")

for _ in range(CALIBRATION_SAMPLES):
    ax, ay, az = imu.accel()
    gx, gy, gz = imu.gyro()

    accel_offset[0] += ax
    accel_offset[1] += ay
    accel_offset[2] += (az - 1.0)  # remove gravity

    gyro_offset[0] += gx
    gyro_offset[1] += gy
    gyro_offset[2] += gz

    time.sleep(0.01)

accel_offset = [v / CALIBRATION_SAMPLES for v in accel_offset]
gyro_offset = [v / CALIBRATION_SAMPLES for v in gyro_offset]

print("✅ IMU calibrated")
print("Accel offset:", accel_offset)
print("Gyro offset:", gyro_offset)

# -----------------------------
# MOTION THRESHOLDS (post-calibration)
# -----------------------------
ACCEL_THRESH = 0.04   # g
GYRO_THRESH = 1.5     # deg/s

def is_moving():
    ax, ay, az = imu.accel()
    gx, gy, gz = imu.gyro()

    # Remove offsets
    ax -= accel_offset[0]
    ay -= accel_offset[1]
    az = (az - 1.0) - accel_offset[2]

    gx -= gyro_offset[0]
    gy -= gyro_offset[1]
    gz -= gyro_offset[2]

    accel_mag = abs(ax) + abs(ay) + abs(az)
    gyro_mag = abs(gx) + abs(gy) + abs(gz)

    return accel_mag > ACCEL_THRESH or gyro_mag > GYRO_THRESH
