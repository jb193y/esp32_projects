# imu.py
from machine import I2C, Pin
import time

_IMU = None
_I2C = None

SCL_PIN = 22
SDA_PIN = 21
I2C_ID = 0
IMU_ADDR = 0x68

def init_imu(retries=5, delay_ms=300):
    """
    Initialize MPU6050 safely with retries.
    Call this explicitly from main.py
    """
    global _IMU, _I2C

    if _IMU:
        return _IMU

    from lib.mpu6050 import MPU6050

    for attempt in range(1, retries + 1):
        try:
            print(f"🧭 IMU init attempt {attempt}")

            _I2C = I2C(
                I2C_ID,
                scl=Pin(SCL_PIN),
                sda=Pin(SDA_PIN),
                freq=100_000
            )

            time.sleep_ms(delay_ms)

            # Optional scan for visibility
            devices = _I2C.scan()
            print("I2C scan:", devices)

            if IMU_ADDR not in devices:
                raise OSError("MPU6050 not found on I2C")

            _IMU = MPU6050(_I2C, addr=IMU_ADDR)

            print("✅ MPU6050 initialized")
            return _IMU

        except Exception as e:
            print("⚠️ IMU init failed:", e)
            _IMU = None
            time.sleep_ms(delay_ms)

    print("❌ IMU failed after retries")
    return None


def is_moving():
    if _IMU is None:
        return False

    ax, ay, az = _IMU.accel()
    gx, gy, gz = _IMU.gyro()

    accel_mag = abs(ax) + abs(ay) + abs(az - 1.0)
    gyro_mag = abs(gx) + abs(gy) + abs(gz)

    return accel_mag > 0.04 or gyro_mag > 1.5


def imu_accel_vector():
    if _IMU is None:
        return 0.0, 0.0
    ax, ay, _ = _IMU.accel()
    return ax, ay
