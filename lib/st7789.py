import time
import framebuf

# Standard ST7789 commands
ST7789_SWRESET = b'\x01'
ST7789_SLPOUT  = b'\x11'
ST7789_NORON   = b'\x13'
ST7789_INVON   = b'\x21'
ST7789_DISPON  = b'\x29'
ST7789_CASET   = b'\x2a'
ST7789_RASET   = b'\x2b'
ST7789_RAMWR   = b'\x2c'
ST7789_MADCTL  = b'\x36'
ST7789_COLMOD  = b'\x3a'

class ST7789:
    def __init__(self, spi, width, height, reset, dc, cs=None, backlight=None,
                 rotation=0, xstart=0, ystart=0):
        self.width = width
        self.height = height
        self.spi = spi
        self.reset = reset
        self.dc = dc
        self.cs = cs
        self.backlight = backlight
        self.rotation = rotation

        # Offsets for panels like TFT200C
        self.xstart = xstart
        self.ystart = ystart

        if self.cs:
            self.cs.init(self.cs.OUT, value=1)
        if self.backlight:
            self.backlight.init(self.backlight.OUT, value=1)
        self.dc.init(self.dc.OUT, value=0)
        self.reset.init(self.reset.OUT, value=1)

        self.init_display()

    def write_cmd(self, cmd):
        if self.cs: self.cs(0)
        self.dc(0)
        self.spi.write(cmd)
        if self.cs: self.cs(1)

    def write_data(self, data):
        if self.cs: self.cs(0)
        self.dc(1)
        self.spi.write(data)
        if self.cs: self.cs(1)
    
    def init_display(self):
        self.reset(0)
        time.sleep_ms(50)
        self.reset(1)
        time.sleep_ms(50)

        self.write_cmd(ST7789_SWRESET)
        time.sleep_ms(150)
        self.write_cmd(ST7789_SLPOUT)
        time.sleep_ms(120)

        self.write_cmd(ST7789_COLMOD)
        self.write_data(b'\x55')  # 16-bit color (RGB565)

        self.write_cmd(ST7789_MADCTL)
        # Rotation handling
        if self.rotation == 0:
            self.write_data(b'\x00')
        elif self.rotation == 1:
            self.write_data(b'\x60')
        elif self.rotation == 2:
            self.write_data(b'\xc0')
        else:
            self.write_data(b'\xa0')

        self.write_cmd(ST7789_INVON)   # IPS screens often need inversion
        self.write_cmd(ST7789_NORON)
        self.write_cmd(ST7789_DISPON)

        if self.backlight:
            self.backlight(1)

    def set_window(self, x0, y0, x1, y1):
        x0 += self.xstart
        x1 += self.xstart
        y0 += self.ystart
        y1 += self.ystart

        self.write_cmd(ST7789_CASET)
        self.write_data(bytearray([x0 >> 8, x0 & 0xFF, x1 >> 8, x1 & 0xFF]))
        self.write_cmd(ST7789_RASET)
        self.write_data(bytearray([y0 >> 8, y0 & 0xFF, y1 >> 8, y1 & 0xFF]))
        self.write_cmd(ST7789_RAMWR)

    def fill(self, color):
        color_hi = color >> 8
        color_lo = color & 0xFF

        self.set_window(0, 0, self.width - 1, self.height - 1)

        chunk_size = 1024
        buffer = bytearray([color_hi, color_lo] * chunk_size)

        pixels_remaining = self.width * self.height
        if self.cs: self.cs(0)
        self.dc(1)
        while pixels_remaining > 0:
            to_write = min(pixels_remaining, chunk_size)
            self.spi.write(buffer[:to_write * 2])
            pixels_remaining -= to_write
        if self.cs: self.cs(1)

    def fill_rect(self, x, y, w, h, color):
        if w <= 0 or h <= 0:
            return

        # clip (basic)
        if x < 0:
            w += x
            x = 0
        if y < 0:
            h += y
            y = 0
        if x + w > self.width:
            w = self.width - x
        if y + h > self.height:
            h = self.height - y
        if w <= 0 or h <= 0:
            return

        color_hi = color >> 8
        color_lo = color & 0xFF

        self.set_window(x, y, x + w - 1, y + h - 1)

        # write in chunks
        chunk_pixels = 1024
        buf = bytearray([color_hi, color_lo] * chunk_pixels)

        total = w * h
        if self.cs: self.cs(0)
        self.dc(1)
        while total > 0:
            n = min(total, chunk_pixels)
            self.spi.write(buf[:n * 2])
            total -= n
        if self.cs: self.cs(1)

    def pixel(self, x, y, color):
        self.set_window(x, y, x, y)
        self.write_data(bytearray([color >> 8, color & 0xFF]))

    def text(self, string, x, y, color=0xFFFF, background=0x0000):
        # 8x8 text using framebuf
        buf = bytearray(8 * 8 * 2)  # 8x8 RGB565
        fb = framebuf.FrameBuffer(buf, 8, 8, framebuf.RGB565)

        for char in string:
            fb.fill(background)
            fb.text(char, 0, 0, color)

            self.set_window(x, y, x + 7, y + 7)
            if self.cs: self.cs(0)
            self.dc(1)
            self.spi.write(buf)
            if self.cs: self.cs(1)
            x += 8
