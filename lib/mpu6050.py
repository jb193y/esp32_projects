# mpu6050.py
import time

class MPU6050:
    def __init__(self, i2c, addr=0x68):
        self.i2c = i2c
        self.addr = addr

        time.sleep(0.1)

        # Wake up MPU6050 (retry-safe)
        for _ in range(5):
            try:
                self.i2c.writeto_mem(self.addr, 0x6B, b'\x00')
                break
            except OSError:
                time.sleep(0.1)
        else:
            raise OSError("MPU6050 wake-up failed")

    def _read(self, reg):
        # Read two bytes
        data = self.i2c.readfrom_mem(self.addr, reg, 2)
        value = (data[0] << 8) | data[1]

        # Convert to signed 16-bit
        if value & 0x8000:
            value = -((65535 - value) + 1)

        return value

    def accel(self):
        return (
            self._read(0x3B) / 16384.0,
            self._read(0x3D) / 16384.0,
            self._read(0x3F) / 16384.0
        )

    def gyro(self):
        return (
            self._read(0x43) / 131.0,
            self._read(0x45) / 131.0,
            self._read(0x47) / 131.0
        )
