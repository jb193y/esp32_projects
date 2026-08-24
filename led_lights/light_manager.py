import machine
import neopixel
import ujson
import os
import time
import math

SCHEDULE_FILE = "schedule.json"

class LightManager:
    def __init__(self, pin_num=48, num_pixels=1):
        self.pin_num = pin_num
        self.num_pixels = num_pixels
        
        # Initialize NeoPixel
        try:
            self.pin = machine.Pin(self.pin_num, machine.Pin.OUT)
            self.np = neopixel.NeoPixel(self.pin, self.num_pixels)
            self.has_np = True
            print(f"NeoPixel initialized on Pin {self.pin_num} with {self.num_pixels} pixels.")
        except Exception as e:
            self.has_np = False
            self.np = None
            print(f"Failed to initialize NeoPixel (falling back to simple GPIO on Pin {self.pin_num}): {e}")

        # Light states
        self.power = True
        self.color = {"r": 255, "g": 255, "b": 255} # default white
        self.pattern = "solid" # solid, rainbow, breathing, strobe, blink
        self.speed = 1.0
        
        # Schedule states
        self.schedule = {
            "sleep_time": "22:00",
            "wakeup_time": "06:00",
            "enabled": False
        }
        self.load_schedule()
        
        # Operational states
        self.last_pattern_tick = 0
        self.last_schedule_check = 0
        self.last_trigger_minute = -1
        self.rainbow_hue = 0.0

    def load_schedule(self):
        try:
            if SCHEDULE_FILE in os.listdir():
                with open(SCHEDULE_FILE, "r") as f:
                    self.schedule = ujson.load(f)
                print("Schedule loaded from filesystem:", self.schedule)
        except Exception as e:
            print("Failed to load schedule from file:", e)

    def save_schedule(self, sched_data):
        try:
            self.schedule.update(sched_data)
            with open(SCHEDULE_FILE, "w") as f:
                ujson.dump(self.schedule, f)
            print("Schedule saved successfully:", self.schedule)
        except Exception as e:
            print("Failed to save schedule to file:", e)

    def set_color_pattern(self, power=None, color=None, pattern=None, speed=None):
        if power is not None:
            self.power = power
        if color is not None:
            # Handle list, dict, or color object
            if isinstance(color, dict):
                self.color = {
                    "r": int(color.get("r", 255)),
                    "g": int(color.get("g", 255)),
                    "b": int(color.get("b", 255))
                }
            elif isinstance(color, (list, tuple)) and len(color) >= 3:
                self.color = {"r": int(color[0]), "g": int(color[1]), "b": int(color[2])}
        if pattern is not None:
            self.pattern = str(pattern).lower()
        if speed is not None:
            self.speed = float(speed)
        
        print(f"Lights updated: power={self.power}, color={self.color}, pattern={self.pattern}, speed={self.speed}")
        
        # If pattern is solid, apply immediately
        if not self.power:
            self.clear_lights()
        elif self.pattern == "solid":
            self.set_all_pixels(self.color["r"], self.color["g"], self.color["b"])

    def clear_lights(self):
        self.set_all_pixels(0, 0, 0)

    def set_all_pixels(self, r, g, b):
        if not self.has_np or not self.np:
            # Simple GPIO fallback (high/low status)
            try:
                if r > 0 or g > 0 or b > 0:
                    self.pin.on()
                else:
                    self.pin.off()
            except:
                pass
            return
        
        try:
            for i in range(self.num_pixels):
                self.np[i] = (r, g, b)
            self.np.write()
        except Exception as e:
            print("NeoPixel write error:", e)

    def hsv_to_rgb(self, h, s, v):
        if s == 0.0:
            return int(v*255), int(v*255), int(v*255)
        i = int(h*6.0)
        f = (h*6.0) - i
        p = v * (1.0 - s)
        q = v * (1.0 - s*f)
        t = v * (1.0 - s*(1.0-f))
        i = i % 6
        if i == 0: return int(v*255), int(t*255), int(p*255)
        if i == 1: return int(q*255), int(v*255), int(p*255)
        if i == 2: return int(p*255), int(v*255), int(t*255)
        if i == 3: return int(p*255), int(q*255), int(v*255)
        if i == 4: return int(t*255), int(p*255), int(v*255)
        if i == 5: return int(v*255), int(p*255), int(q*255)
        return 0, 0, 0

    def update(self):
        now_ms = time.ticks_ms()
        
        # 1. Schedule checks (every 10 seconds)
        if time.ticks_diff(now_ms, self.last_schedule_check) > 10000:
            self.last_schedule_check = now_ms
            self.check_schedule()

        # 2. Dynamic pattern animations (if power is ON)
        if not self.power:
            return

        if self.pattern == "solid":
            return

        # Handle animation cycles based on speed
        interval = int(100 / max(0.1, self.speed))
        if time.ticks_diff(now_ms, self.last_pattern_tick) > interval:
            self.last_pattern_tick = now_ms
            self.animate(now_ms)

    def animate(self, now_ms):
        if self.pattern == "rainbow":
            # Color cycle
            self.rainbow_hue += 0.02 * self.speed
            if self.rainbow_hue > 1.0:
                self.rainbow_hue -= 1.0
            r, g, b = self.hsv_to_rgb(self.rainbow_hue, 1.0, 1.0)
            self.set_all_pixels(r, g, b)
            
        elif self.pattern == "breathing":
            # Brightness pulse
            pulse = (math.sin(now_ms / 1000.0 * self.speed) + 1.0) / 2.0 # 0.0 to 1.0
            r = int(self.color["r"] * pulse)
            g = int(self.color["g"] * pulse)
            b = int(self.color["b"] * pulse)
            self.set_all_pixels(r, g, b)
            
        elif self.pattern == "strobe":
            # Quick flashing
            pulse = int(now_ms / 150) % 2
            if pulse == 0:
                self.set_all_pixels(self.color["r"], self.color["g"], self.color["b"])
            else:
                self.clear_lights()
                
        elif self.pattern == "blink":
            # Gentle flashing
            pulse = int(now_ms / 600) % 2
            if pulse == 0:
                self.set_all_pixels(self.color["r"], self.color["g"], self.color["b"])
            else:
                self.clear_lights()

    def check_schedule(self):
        if not self.schedule.get("enabled", False):
            return

        try:
            # Check if RTC time is valid (year > 2020)
            lt = time.localtime()
            if lt[0] < 2024:
                # Time not synced yet
                return

            current_time_str = f"{lt[3]:02d}:{lt[4]:02d}"
            current_minute = lt[4]

            # Avoid re-triggering within the same minute
            if current_minute == self.last_trigger_minute:
                return

            sleep_time = self.schedule.get("sleep_time")
            wakeup_time = self.schedule.get("wakeup_time")

            if current_time_str == sleep_time:
                print(f"[Schedule] Sleep time reached ({sleep_time}). Turning lights OFF.")
                self.last_trigger_minute = current_minute
                self.set_color_pattern(power=False)
                
            elif current_time_str == wakeup_time:
                print(f"[Schedule] Wakeup time reached ({wakeup_time}). Turning lights ON.")
                self.last_trigger_minute = current_minute
                self.set_color_pattern(power=True)
        except Exception as e:
            print("Error checking light schedule:", e)
