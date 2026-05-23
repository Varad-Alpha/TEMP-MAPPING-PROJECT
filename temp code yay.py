import board
import bitbangio
import analogio
import time
import math
import adafruit_bmp280

# ─── PCF8574 LCD DRIVER ──────────────────────────────────────────
class PCF8574_LCD:
    RS = 0x01
    EN = 0x04
    BL = 0x08

    def __init__(self, i2c, address=0x27):
        self._i2c  = i2c
        self._addr = address
        self._bl   = self.BL
        self._init_lcd()

    def _write_bus(self, data):
        while not self._i2c.try_lock():
            pass
        try:
            self._i2c.writeto(self._addr, bytes([data]))
        finally:
            self._i2c.unlock()

    def _strobe(self, data):
        self._write_bus(data | self.EN)
        time.sleep(0.0005)
        self._write_bus(data & ~self.EN)
        time.sleep(0.0001)

    def _write_nibble(self, nibble, mode=0):
        self._strobe((nibble & 0xF0) | mode | self._bl)

    def _write_byte(self, byte, mode=0):
        self._write_nibble(byte & 0xF0, mode)
        self._write_nibble((byte << 4) & 0xF0, mode)
        time.sleep(0.002)

    def _init_lcd(self):
        time.sleep(0.05)
        self._write_nibble(0x30); time.sleep(0.005)
        self._write_nibble(0x30); time.sleep(0.001)
        self._write_nibble(0x30); time.sleep(0.001)
        self._write_nibble(0x20); time.sleep(0.001)
        self._write_byte(0x28)
        self._write_byte(0x0C)
        self._write_byte(0x06)
        self._write_byte(0x01)
        time.sleep(0.005)

    def create_char(self, location, charmap):
        self._write_byte(0x40 | ((location & 0x07) << 3))
        for row in charmap:
            self._write_byte(row, self.RS)

    @property
    def backlight(self):
        return self._bl == self.BL

    @backlight.setter
    def backlight(self, on):
        self._bl = self.BL if on else 0
        self._write_bus(self._bl)

    def clear(self):
        self._write_byte(0x01)
        time.sleep(0.002)

    def cursor_position(self, col, row):
        self._write_byte(0x80 | (col + [0x00, 0x40][row]))

    @property
    def message(self):
        return ""

    @message.setter
    def message(self, text):
        for ch in str(text):
            self._write_byte(ord(ch), self.RS)

# ─── PIN SETUP ───────────────────────────────────────────────────
i2c = bitbangio.I2C(board.GP1, board.GP0)
mq2 = analogio.AnalogIn(board.GP27)

# ─── SENSORS ─────────────────────────────────────────────────────
bmp = adafruit_bmp280.Adafruit_BMP280_I2C(i2c, address=0x76)
lcd = PCF8574_LCD(i2c, address=0x27)

bmp.mode                 = adafruit_bmp280.MODE_NORMAL
bmp.standby_period       = adafruit_bmp280.STANDBY_TC_500
bmp.iir_filter           = adafruit_bmp280.IIR_FILTER_X16
bmp.overscan_pressure    = adafruit_bmp280.OVERSCAN_X16
bmp.overscan_temperature = adafruit_bmp280.OVERSCAN_X2

# ─── CONFIG ──────────────────────────────────────────────────────
LOG_INTERVAL       = 1.0
AUTO_SAVE_INTERVAL = 60.0
SPIKE_THRESHOLD    = 20
LCD_CYCLE_TIME     = 3.0
DATA_FILE          = "/data.csv"

FULL_BLOCK_CHAR = [0x1F, 0x1F, 0x1F, 0x1F, 0x1F, 0x1F, 0x1F, 0x1F]

# ─── STATE ───────────────────────────────────────────────────────
readings      = []
total_records = 0
start_time    = time.monotonic()
last_aqi      = None
smoke_alert   = False
lcd_screen    = 0
last_lcd_swap = time.monotonic()

latest = {
    "temp": 0.0, "pressure": 0.0, "aqi": 0,
    "comfort": 0, "mq2_raw": 0, "count": 0
}

# ─── COMFORT SCORE ───────────────────────────────────────────────
def compute_comfort(temp, pressure, aqi):
    temp_penalty     = 0 if 22 <= temp <= 26 else min(40, abs(temp - 24) * 4)
    aqi_penalty      = min(40, aqi * 0.4)
    pressure_penalty = 0 if 1005 <= pressure <= 1020 else min(20, abs(pressure - 1013) * 0.5)
    return max(0, min(100, int(100 - temp_penalty - aqi_penalty - pressure_penalty)))

# ─── HELPERS ─────────────────────────────────────────────────────
def lcd_pad(text, width=16):
    s = str(text)
    return s + " " * (width - len(s)) if len(s) < width else s[:width]

def lcd_show(line1, line2=""):
    lcd.cursor_position(0, 0)
    lcd.message = lcd_pad(line1)
    lcd.cursor_position(0, 1)
    lcd.message = lcd_pad(line2)

def comfort_bar(score):
    filled = int(score / 100 * 14)
    return "[" + chr(0) * filled + " " * (14 - filled) + "]"

def read_mq2():
    raw = mq2.value
    return raw, int((raw / 65535) * 100)

def detect_spike(new_aqi):
    global last_aqi
    if last_aqi is None:
        last_aqi = new_aqi
        return False
    spike = (new_aqi - last_aqi) >= SPIKE_THRESHOLD
    last_aqi = new_aqi
    return spike

def lcd_flash_alert(message, times=3):
    for _ in range(times):
        lcd_show("!! ALERT !!", message)
        time.sleep(0.4)
        lcd_show("", "")
        time.sleep(0.3)
    lcd_show("!! ALERT !!", message)
    time.sleep(2)

def cycle_lcd():
    global lcd_screen
    t, p, aqi    = latest["temp"], latest["pressure"], latest["aqi"]
    comfort, raw = latest["comfort"], latest["mq2_raw"]
    count        = latest["count"]
    air          = "Good" if aqi < 30 else ("Mod" if aqi < 60 else "Poor")

    if lcd_screen == 0:
        lcd_show(f"T:{t}C #{count}", f"Air:{air} P:{int(p)}")
    elif lcd_screen == 1:
        lcd_show(f"Comfort {comfort:3d}/100", comfort_bar(comfort))
    elif lcd_screen == 2:
        lcd_show(f"Smoke:{'ALERT!' if smoke_alert else 'Safe ':5}", f"MQ2 raw:{raw}")

    lcd_screen = (lcd_screen + 1) % 3

# ─── FLASH STORAGE (CSV) ─────────────────────────────────────────
def init_csv_file():
    """Ensures a clean CSV header exists if the file doesn't exist yet."""
    try:
        with open(DATA_FILE, "a") as f:
            # If the file is completely brand new, write headers
            if f.tell() == 0:
                f.write("time_sec,temperature_c,pressure_hpa,mq2_raw,aqi,comfort\n")
    except Exception as e:
        print("Storage write-protection active:", e)

def save_to_flash():
    """Appends current short-term session readings to flash and clears RAM."""
    global readings
    if not readings:
        return True
    try:
        # Open in append mode 'a' so we don't erase previous history
        with open(DATA_FILE, "a") as f:
            for r in readings:
                f.write(f"{r[0]},{r[1]},{r[2]},{r[3]},{r[4]},{r[5]}\n")
        readings = []  # Flush RAM data now that it is committed to silicon
        return True
    except Exception as e:
        print("Save error (Is GP2 Jumper removed?):", e)
        return False

# ─── BOOT SEQUENCE ───────────────────────────────────────────────
def boot_animation(duration=2.0):
    spinner = ["|", "/", "-", "\\"]
    frame   = 0
    start   = time.monotonic()
    while True:
        elapsed = time.monotonic() - start
        if elapsed >= duration:
            break
        progress = int((elapsed / duration) * 14)
        bar_line = "[" + chr(0) * progress + "." * (14 - progress) + "]"
        lcd_show(f"  ** EM-11 **  {spinner[frame % 4]}", bar_line)
        frame += 1
        time.sleep(0.12)

def run_boot_sequence():
    lcd.create_char(0, FULL_BLOCK_CHAR)
    boot_animation(duration=2.0)
    lcd.clear()
    lcd_show(" Made by Varad", "      9C")
    time.sleep(2.0)
    lcd.clear()
    lcd_show("EM 11 BOOTING", "UP...          ")
    time.sleep(1.5)

# ─── RUN BOOT & CSV SETUP ────────────────────────────────────────
run_boot_sequence()
init_csv_file()

lcd_show("Mapping...", "Walk around!")
time.sleep(1)
print("EM 11 — Classroom Environment Mapper started.")

# ─── MAIN LOOP ───────────────────────────────────────────────────
last_log      = time.monotonic()
last_autosave = time.monotonic()

while True:
    now = time.monotonic()

    if now - last_log >= LOG_INTERVAL:
        elapsed  = round(now - start_time, 1)
        temp     = round(bmp.temperature, 2)
        pressure = round(bmp.pressure, 2)
        raw, aqi = read_mq2()
        comfort  = compute_comfort(temp, pressure, aqi)

        if detect_spike(aqi):
            smoke_alert = True
            lcd_flash_alert("Smoke/Gas!")
        else:
            smoke_alert = False

        readings.append((elapsed, temp, pressure, raw, aqi, comfort))
        total_records += 1
        
        latest.update({
            "temp": temp, "pressure": pressure, "aqi": aqi,
            "comfort": comfort, "mq2_raw": raw, "count": total_records
        })

        print(f"[{elapsed}s] T:{temp}C AQI:{aqi} P:{pressure}hPa Comfort:{comfort}")
        last_log = now

    if now - last_lcd_swap >= LCD_CYCLE_TIME and not smoke_alert:
        cycle_lcd()
        last_lcd_swap = now

    if now - last_autosave >= AUTO_SAVE_INTERVAL:
        lcd_show("Auto-saving...", f"{len(readings)} cache")
        print("Auto-save triggered...")
        if save_to_flash():
            lcd_show("Saved!", "Data logged OK")
        else:
            lcd_show("Write Failed!", "Check Jumper Pin")
        time.sleep(1.5)
        lcd_show("Mapping...", "Walk around!")
        last_autosave = now
        last_lcd_swap = time.monotonic()

    time.sleep(0.05)