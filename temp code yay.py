"""
JCJenson (in SPAAAACE!) Inc.
Environmental Monitor Unit - JCJ-EM11
CircuitPython - Raspberry Pi Pico H
ESP8266 Wi-Fi via UART AT commands (GP8=TX, GP9=RX)
Start/Stop logging via WiFi dashboard
"""

import board
import busio
import analogio
import digitalio
import time
import storage
import supervisor

import adafruit_bmp280
# NOTE: MPU6050 is wired but its data is not logged; imported only to keep
# the I2C device from holding the bus in an uninitialised state.
import adafruit_mpu6050

# ─── PIN SETUP ───────────────────────────────────────────────────
i2c  = busio.I2C(board.GP1, board.GP0, frequency=100000)
mq2  = analogio.AnalogIn(board.GP28)
uart = busio.UART(board.GP8, board.GP9, baudrate=115200)

led = digitalio.DigitalInOut(board.LED)
led.direction = digitalio.Direction.OUTPUT

# ─── SENSORS ─────────────────────────────────────────────────────
bmp = adafruit_bmp280.Adafruit_BMP280_I2C(i2c, address=0x76)
mpu = adafruit_mpu6050.MPU6050(i2c, address=0x68)   # initialised to avoid bus lockup

bmp.mode                 = adafruit_bmp280.MODE_NORMAL
bmp.standby_period       = adafruit_bmp280.STANDBY_TC_500
bmp.iir_filter           = adafruit_bmp280.IIR_FILTER_X16
bmp.overscan_pressure    = adafruit_bmp280.OVERSCAN_X16
bmp.overscan_temperature = adafruit_bmp280.OVERSCAN_X2

# ─── CONFIG ──────────────────────────────────────────────────────
WIFI_SSID      = "JCJ-EM11"
WIFI_PASSWORD  = "jcjenson1"
LOG_INTERVAL   = 3.0
SPIKE_THRESH   = 20
DATA_FILE      = "/data.csv"
SERVER_IP      = "192.168.4.1"   # ESP8266 default AP address
MAX_READINGS   = 500             # cap to avoid MemoryError (~100 KB at this size)
ESP_INIT_TRIES = 3               # how many times to retry esp_init on failure

# ─── STATE ───────────────────────────────────────────────────────
readings    = []
start_time  = None
last_aqi    = None
is_logging  = False   # renamed from 'logging' to avoid shadowing stdlib name
saved       = False

# ─── LED ─────────────────────────────────────────────────────────
def led_blink(times, on=0.1, off=0.1):
    for _ in range(times):
        led.value = True;  time.sleep(on)
        led.value = False; time.sleep(off)

def led_heartbeat():
    led.value = True;  time.sleep(0.08)
    led.value = False; time.sleep(0.08)
    led.value = True;  time.sleep(0.08)
    led.value = False

def led_slow_blink():
    led.value = True;  time.sleep(0.05)
    led.value = False

def led_rapid_brief():
    """Short non-blocking spike indicator (4 fast flashes, ~0.4 s total)."""
    for _ in range(4):
        led.value = True;  time.sleep(0.05)
        led.value = False; time.sleep(0.05)

def led_solid(duration=0.5):
    led.value = True; time.sleep(duration); led.value = False

def led_boot():
    led_blink(3, 0.1, 0.1)
    time.sleep(0.2)
    led_blink(3, 0.1, 0.1)

def led_error_halt():
    """Rapid continuous flash — called on unrecoverable boot failure."""
    while True:
        led.value = True;  time.sleep(0.05)
        led.value = False; time.sleep(0.05)

# ─── COMFORT ─────────────────────────────────────────────────────
def compute_comfort(temp, pressure, aqi):
    tp = 0 if 22 <= temp <= 26 else min(40, abs(temp - 24) * 4)
    ap = min(40, aqi * 0.4)
    pp = 0 if 1005 <= pressure <= 1020 else min(20, abs(pressure - 1013) * 0.5)
    return max(0, min(100, int(100 - tp - ap - pp)))

def comfort_label(score):
    if score >= 75: return "NOMINAL"
    if score >= 55: return "ACCEPTABLE"
    if score >= 35: return "MARGINAL"
    return "HAZARDOUS"

# ─── SENSORS ─────────────────────────────────────────────────────
def read_mq2():
    raw = mq2.value
    return raw, int((raw / 65535) * 100)

def detect_spike(new_aqi):
    global last_aqi
    if last_aqi is None:
        last_aqi = new_aqi
        return False
    spike    = (new_aqi - last_aqi) >= SPIKE_THRESH
    last_aqi = new_aqi
    return spike

# ─── STORAGE ─────────────────────────────────────────────────────
def save_to_flash():
    if supervisor.runtime.usb_connected:
        dump_data()
        return True
    try:
        storage.remount("/", readonly=False)
        with open(DATA_FILE, "w") as f:
            f.write("time_sec,temperature_c,pressure_hpa,mq2_raw,aqi,comfort\n")
            for r in readings:
                f.write("{},{},{},{},{},{}\n".format(
                    r[0], r[1], r[2], r[3], r[4], r[5]))
        storage.remount("/", readonly=True)
        return True
    except MemoryError:
        print("[EM11] Save error: out of memory")
        return False
    except Exception as e:
        print("[EM11] Save error: {}".format(e))
        return False

def dump_data():
    print("=== CLASSROOM ENVIRONMENT DATA ===")
    print("time_sec,temperature_c,pressure_hpa,mq2_raw,aqi,comfort")
    for r in readings:
        print("{},{},{},{},{},{}".format(r[0], r[1], r[2], r[3], r[4], r[5]))
    print("=== END OF DATA ===")
    print("Total readings: {}".format(len(readings)))

# ─── ESP8266 AT DRIVER ───────────────────────────────────────────
def safe_decode(buf):
    """Decode bytes keeping only ASCII < 128.
    Avoids all codec / keyword-arg issues in CircuitPython.
    Iterating bytes in CircuitPython yields ints, so chr() is correct."""
    return "".join(chr(b) for b in buf if b < 128)

def at(cmd, wait="OK", timeout=5.0):
    """Send an AT command; return (success, response_lines).
    Checks for the expected token in the raw byte buffer before decoding
    so non-ASCII boot garbage never reaches the decode step."""
    uart.write((cmd + "\r\n").encode())
    deadline = time.monotonic() + timeout
    buf = b""
    while time.monotonic() < deadline:
        chunk = uart.read(128)
        if chunk:
            buf += chunk
            # Check on raw bytes — avoids any decode error entirely
            if wait.encode() in buf or b"ERROR" in buf:
                break
        time.sleep(0.01)
    text  = safe_decode(buf)
    lines = text.strip().splitlines()
    ok    = wait in text
    return ok, lines

def at_raw(data, timeout=3.0):
    """Write raw bytes (e.g. CIPSEND payload) and drain the response."""
    uart.write(data if isinstance(data, bytes) else data.encode())
    deadline = time.monotonic() + timeout
    buf = b""
    while time.monotonic() < deadline:
        chunk = uart.read(128)
        if chunk:
            buf += chunk
        time.sleep(0.01)
    return buf

def esp_init():
    """Bring up ESP8266 as a WiFi AP with a TCP server on port 80.
    Returns True on success, False on failure."""
    print("[ESP] Resetting...")
    ok, _ = at("AT+RST", wait="ready", timeout=8.0)
    if not ok:
        # Some firmwares emit "ready" buried in boot ROM text at odd timing;
        # flush any leftovers and continue rather than aborting.
        time.sleep(3)
        uart.read(uart.in_waiting or 256)   # drain boot noise

    at("ATE0")                              # turn echo off
    at("AT+CWMODE=2")                       # AP-only mode

    ok, _ = at('AT+CWSAP="{}","{}",6,4'.format(WIFI_SSID, WIFI_PASSWORD),
               timeout=6.0)
    if not ok:
        print("[ESP] WARNING: CWSAP may have failed — continuing")

    at("AT+CIPMUX=1")                       # multi-connection mode (required for server)

    ok, resp = at("AT+CIPSERVER=1,80", timeout=5.0)
    if ok:
        print("[ESP] AP + TCP server ready  http://{}".format(SERVER_IP))
    else:
        print("[ESP] CIPSERVER failed: {}".format(resp))
    return ok

def esp_read_request(timeout=0.08):
    """Non-blocking UART drain; returns (link_id, request_line) or (None, None).
    The ESP8266 frames incoming TCP data as: +IPD,<id>,<len>:<payload>"""
    buf = b""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        chunk = uart.read(256)
        if chunk:
            buf += chunk
        time.sleep(0.005)   # yield CPU; avoids starving the logging timer

    if not buf:
        return None, None

    # Work on raw bytes for the +IPD header search to avoid decode issues
    ipd_pos = buf.find(b"+IPD,")
    if ipd_pos == -1:
        return None, None

    try:
        colon_pos = buf.index(b":", ipd_pos)
        header    = safe_decode(buf[ipd_pos + 5 : colon_pos])  # "id,len"
        header_parts = header.split(",")
        link_id   = int(header_parts[0])
        payload   = safe_decode(buf[colon_pos + 1:])
        first_line = payload.splitlines()[0] if payload else ""
        return link_id, first_line
    except Exception as e:
        print("[ESP] IPD parse error: {}".format(e))
        return None, None

def esp_send(link_id, body, content_type="application/json", status="200 OK"):
    """Send a complete HTTP response and close the connection.
    Content-Length is computed from the encoded body bytes, not the str length."""
    body_bytes = body.encode()
    headers = (
        "HTTP/1.1 {}\r\n"
        "Content-Type: {}\r\n"
        "Content-Length: {}\r\n"
        "Access-Control-Allow-Origin: *\r\n"
        "Connection: close\r\n\r\n"
    ).format(status, content_type, len(body_bytes))
    payload = headers.encode() + body_bytes
    ok, _ = at("AT+CIPSEND={},{}".format(link_id, len(payload)),
               wait=">", timeout=5.0)
    if ok:
        at_raw(payload, timeout=5.0)
    else:
        print("[ESP] CIPSEND prompt not received for link {}".format(link_id))
    # Always attempt to close — even if send failed, to free the connection slot
    at("AT+CIPCLOSE={}".format(link_id), timeout=3.0)

def serve_html(link_id):
    try:
        with open("/index.html", "r") as f:
            html = f.read()
        esp_send(link_id, html, content_type="text/html")
    except MemoryError:
        esp_send(link_id,
                 "<h1>Out of memory</h1><p>index.html too large to load.</p>",
                 content_type="text/html", status="500 Internal Server Error")
    except Exception as e:
        esp_send(link_id,
                 "<h1>index.html missing</h1><p>{}</p>".format(e),
                 content_type="text/html", status="404 Not Found")

# ─── REQUEST ROUTER ───────────────────────────────────────────────
def handle_request(link_id, request_line):
    global is_logging, readings, start_time, last_aqi, saved

    # Parse "GET /path HTTP/1.1"
    req_parts = request_line.split()
    if len(req_parts) < 2:
        esp_send(link_id, '{"error":"bad request"}', status="400 Bad Request")
        return
    path = req_parts[1]

    # Strip query strings (?foo=bar) so dashboard JS params don't cause 404s
    if "?" in path:
        path = path.split("?")[0]

    if path == "/" or path == "/index.html":
        serve_html(link_id)

    elif path == "/api/start":
        readings   = []
        last_aqi   = None
        saved      = False
        start_time = time.monotonic()
        is_logging = True
        print("[EM11] MISSION STARTED")
        esp_send(link_id, '{"status":"started"}')

    elif path == "/api/stop":
        is_logging = False
        print("[EM11] MISSION STOPPED | {} readings".format(len(readings)))
        led_solid(1.0)
        led_blink(3, 0.1, 0.1)
        esp_send(link_id, '{"status":"stopped"}')

    elif path == "/api/save":
        if save_to_flash():
            saved = True
            print("[EM11] Saved to flash!")
            esp_send(link_id, '{"status":"saved"}')
        else:
            esp_send(link_id, '{"status":"error"}')

    elif path == "/api/status":
        esp_send(link_id,
            '{{"logging":{},"count":{},"saved":{},"full":{}}}'.format(
                "true" if is_logging else "false",
                len(readings),
                "true" if saved else "false",
                "true" if len(readings) >= MAX_READINGS else "false"
            ))

    elif path == "/api/latest":
        if not readings or not is_logging:
            esp_send(link_id, '{"empty":true}')
        else:
            r = readings[-1]
            esp_send(link_id,
                '{{"time":{},"temp":{},"pressure":{},'
                '"mq2":{},"aqi":{},"comfort":{},'
                '"count":{},"label":"{}"}}'.format(
                    r[0], r[1], r[2], r[3], r[4], r[5],
                    len(readings), comfort_label(r[5])))

    elif path == "/api/all":
        if not readings:
            esp_send(link_id, "[]")
        else:
            # Build JSON array without f-strings to keep memory usage low
            row_parts = ["[{},{},{},{},{},{}]".format(
                r[0], r[1], r[2], r[3], r[4], r[5]) for r in readings]
            esp_send(link_id, "[" + ",".join(row_parts) + "]")

    else:
        esp_send(link_id, '{"error":"not found"}', status="404 Not Found")

# ─── BOOT ─────────────────────────────────────────────────────────
print("=" * 45)
print("  JCJenson (in SPAAAACE!) Inc.")
print("  Environmental Monitor Unit")
print("  Model: JCJ-EM11 | SN: 2025-CHN")
print('  "We Care About Your Safety*"')
print("  *JCJenson not liable for injury/death")
print("=" * 45)

led_boot()

# Retry ESP init up to ESP_INIT_TRIES times before giving up
for attempt in range(1, ESP_INIT_TRIES + 1):
    print("[ESP] Init attempt {}/{}".format(attempt, ESP_INIT_TRIES))
    if esp_init():
        break
    print("[ESP] Retrying in 3 s...")
    time.sleep(3)
else:
    print("[ESP] FATAL: Could not bring up ESP8266 after {} attempts.".format(ESP_INIT_TRIES))
    print("[ESP] Check wiring: GP8->RX, GP9->TX, CH_PD->3V3, correct baud.")
    led_error_halt()   # blinks forever so the problem is obvious

print("[EM11] Connect to WiFi: {} | Pass: {}".format(WIFI_SSID, WIFI_PASSWORD))
print("[EM11] Dashboard: http://{}".format(SERVER_IP))
print("[EM11] All systems online. Waiting for mission start...")
print("-" * 45)

# ─── MAIN LOOP ───────────────────────────────────────────────────
last_log       = 0
last_heartbeat = 0

while True:
    now = time.monotonic()

    # ── Poll ESP8266 for incoming HTTP requests ───────────────────
    link_id, req_line = esp_read_request()
    if link_id is not None and req_line:
        try:
            handle_request(link_id, req_line)
        except MemoryError:
            print("[EM11] MemoryError handling request — readings: {}".format(len(readings)))
            try:
                esp_send(link_id, '{"error":"out of memory"}',
                         status="500 Internal Server Error")
            except Exception:
                pass
        except Exception as e:
            print("[EM11] Request error: {}".format(e))

    # ── Standby heartbeat ─────────────────────────────────────────
    if not is_logging:
        if now - last_heartbeat >= 1.5:
            led_heartbeat()
            last_heartbeat = now

    # ── Active logging ────────────────────────────────────────────
    else:
        if now - last_log >= LOG_INTERVAL:
            # Guard against RAM exhaustion
            if len(readings) >= MAX_READINGS:
                print("[EM11] WARNING: MAX_READINGS ({}) reached. Stopping log.".format(
                    MAX_READINGS))
                is_logging = False
            else:
                elapsed  = round(now - start_time, 1)
                temp     = round(bmp.temperature, 2)
                pressure = round(bmp.pressure, 2)
                raw, aqi = read_mq2()
                comfort  = compute_comfort(temp, pressure, aqi)

                if detect_spike(aqi):
                    print("[EM11] WARNING: AQI spike detected: {}".format(aqi))
                    led_rapid_brief()   # ~0.4 s, not 2 s — keeps server responsive
                else:
                    led_slow_blink()

                readings.append((elapsed, temp, pressure, raw, aqi, comfort))
                print("[EM11] #{:03d} | T:{}C | P:{}hPa | AQI:{} | Comfort:{} ({})".format(
                    len(readings), temp, pressure, aqi, comfort, comfort_label(comfort)))
                last_log = now

    time.sleep(0.01)
