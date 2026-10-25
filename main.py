import time
import network
import ubinascii
import machine
from machine import Pin, ADC, PWM, SoftI2C
import dht
import framebuf
from umqtt.simple import MQTTClient


# KONFIGURASI JARINGAN & BROKER MQTT
WIFI_SSID = "Wokwi-GUEST"
WIFI_PASS = ""

BROKER_HOST = "y16d2f91.ala.asia-southeast1.emqxsl.com"
BROKER_PORT = 8883
BROKER_USER = "adamnirvan"
BROKER_PASS = "orichicrist"

DEVICE_NAME   = "esp32-lab-01"
TOPIC_DATA    = f"iot/{DEVICE_NAME}/telemetry".encode()
TOPIC_CONTROL = f"iot/{DEVICE_NAME}/cmd/led".encode()

# KONFIGURASI PIN
PIN_DHT    = 15
PIN_LED_R  = 2    # Merah  (Buruk)
PIN_LED_Y  = 4    # Kuning (Sedang)
PIN_LED_G  = 5    # Hijau  (Baik)
PIN_LDR    = 34
PIN_GAS    = 35
PIN_SERVO  = 18
PIN_BUZZER = 19
PIN_SCL    = 22
PIN_SDA    = 21

# PARAMETER SISTEM
READ_MS           = 1000     
PUB_MS            = 5000     
RETRY_MS          = 5000     
BLINK_MS          = 250     
FILTER_SIZE       = 3        

WARN_DELTA        = 100    
                            
WARN_HOLD_MS      = 10000    
HYST              = 100     
TEMP_HOT          = 32.0     
MANUAL_TIMEOUT_MS = 300000   

# STATE SISTEM
system_mode = "AUTO"
manual_since = 0
led_state   = "OFF"
vent_state  = "CLOSED"
servo_angle = -1     

# Kalibrasi dinamis
gas_baseline  = 0
gas_th_sedang = 1000
gas_th_buruk  = 2000
prev_air_val  = None 
early_warning = False
warn_until    = 0

# Data sensor terakhir
temp = 0.0
hum = 0.0
light_val = 0
air_val = 0
air_stat = "BAIK"
have_data = False

# Status koneksi
mqtt_client = None
mqtt_ok = False

# Buffer moving average
buf_temp, buf_hum, buf_light, buf_air = [], [], [], []

# DRIVER OLED SSD1306
class OledSSD1306(framebuf.FrameBuffer):
    def __init__(self, width, height, i2c, addr=0x3C):
        self.width = width
        self.height = height
        self.i2c = i2c
        self.addr = addr
        self.buffer = bytearray((height // 8) * width)
        super().__init__(self.buffer, width, height, framebuf.MONO_VLSB)
        self._init_display()

    def _cmd(self, cmd_byte):
        self.i2c.writeto(self.addr, bytearray([0x80, cmd_byte]))

    def _init_display(self):
        init_seq = (
            0xAE, 0x20, 0x00, 0x40, 0xA1, 0xA8, self.height - 1,
            0xC8, 0xD3, 0x00, 0xDA, 0x12, 0xD5, 0x80, 0xD9, 0xF1,
            0xDB, 0x30, 0x81, 0xFF, 0xA4, 0xA6, 0x8D, 0x14, 0xAF
        )
        for cmd in init_seq:
            self._cmd(cmd)
        self.fill(0)
        self.show()

    def show(self):
        for cmd in (0x21, 0, self.width - 1, 0x22, 0, (self.height // 8) - 1):
            self._cmd(cmd)
        self.i2c.writeto(self.addr, b"\x40" + self.buffer)

# INISIALISASI SENSOR & AKTUATOR
dht_sensor = dht.DHT22(Pin(PIN_DHT))
led_r = Pin(PIN_LED_R, Pin.OUT, value=0)
led_y = Pin(PIN_LED_Y, Pin.OUT, value=0)
led_g = Pin(PIN_LED_G, Pin.OUT, value=0)

ldr_adc = ADC(Pin(PIN_LDR))
ldr_adc.atten(ADC.ATTN_11DB)
ldr_adc.width(ADC.WIDTH_12BIT)

gas_adc = ADC(Pin(PIN_GAS))
gas_adc.atten(ADC.ATTN_11DB)
gas_adc.width(ADC.WIDTH_12BIT)

servo_pwm = PWM(Pin(PIN_SERVO), freq=50)
buzzer_pwm = PWM(Pin(PIN_BUZZER), freq=1000, duty=0)
i2c_bus = SoftI2C(scl=Pin(PIN_SCL), sda=Pin(PIN_SDA), freq=400000)
oled = OledSSD1306(128, 64, i2c_bus, addr=0x3C)

# FUNGSI BANTU AKTUATOR
def set_servo_angle(angle):
    """Set sudut servo 0-180. Tidak menulis ulang PWM kalau sudut sama."""
    global servo_angle
    angle = max(0, min(180, int(angle)))
    if angle == servo_angle:
        return
    servo_angle = angle
    servo_pwm.duty(int(26 + (angle / 180.0) * 102))


def set_buzzer(active, freq=1200):
    if active:
        buzzer_pwm.freq(freq)
        buzzer_pwm.duty(512)
    else:
        buzzer_pwm.duty(0)


def set_leds(r, y, g):
    led_r.value(r)
    led_y.value(y)
    led_g.value(g)


def moving_average(buf, new_val):
    buf.append(new_val)
    if len(buf) > FILTER_SIZE:
        buf.pop(0)
    return sum(buf) / len(buf)


def map_value(x, in_min, in_max, out_min, out_max):
    """Pemetaan linear proporsional dengan clamp."""
    if in_max == in_min:
        return out_min
    val = int((x - in_min) * (out_max - out_min) / (in_max - in_min) + out_min)
    return max(min(val, out_max), out_min)

# LOGIKA STATUS UDARA
def update_air_status(stat, v):
    if stat == "BAIK":
        if v >= gas_th_buruk:
            return "BURUK"
        if v >= gas_th_sedang:
            return "SEDANG"
        return "BAIK"
    if stat == "SEDANG":
        if v >= gas_th_buruk:
            return "BURUK"
        if v < gas_th_sedang - HYST:
            return "BAIK"
        return "SEDANG"
    if v < gas_th_buruk - HYST:
        if v < gas_th_sedang - HYST:
            return "BAIK"
        return "SEDANG"
    return "BURUK"


# LOGIKA OTOMATIS
# Prioritas: BURUK > EARLY WARNING > SEDANG > BAIK, lalu override suhu
def apply_auto():
    global led_state, vent_state

    if air_stat == "BURUK":
        set_leds(1, 0, 0)
        led_state = "RED"
        angle = 180
        vent_state = "OPEN_MAX(180)"
        set_buzzer(True, 1500)

    elif early_warning:
        # LED kuning di-blink oleh loop utama
        led_r.value(0)
        led_g.value(0)
        led_state = "WARNING_BLINK"
        angle = 90
        vent_state = "PRE-OPEN(90)"
        set_buzzer(True, 2000)

    elif air_stat == "SEDANG":
        set_leds(0, 1, 0)
        led_state = "YELLOW"
        angle = map_value(air_val, gas_th_sedang, gas_th_buruk, 0, 180)
        vent_state = f"PROP({angle})"
        set_buzzer(False)

    else: 
        set_leds(0, 0, 1)
        led_state = "GREEN"
        angle = 0
        vent_state = "CLOSED(0)"
        set_buzzer(False)

    if temp > TEMP_HOT and not early_warning and air_stat != "BURUK":
        angle = 180
        vent_state = "TEMP_OPEN(180)"

    set_servo_angle(angle)

# MODE MANUAL
def enter_manual():
    global system_mode, manual_since
    system_mode = "MANUAL"
    manual_since = time.ticks_ms()


def check_manual_timeout(now):
    """Kalau terlalu lama di MANUAL, kembali ke AUTO supaya sistem tidak 'lupa' menjaga udara."""
    global system_mode
    if system_mode == "MANUAL" and time.ticks_diff(now, manual_since) >= MANUAL_TIMEOUT_MS:
        system_mode = "AUTO"
        print("[MODE] Timeout MANUAL -> AUTO")
        if have_data:
            apply_auto()


def on_mqtt_message(topic, msg_bytes):
    global system_mode, led_state, vent_state
    try:
        cmd = msg_bytes.decode("utf-8").strip().upper()
    except Exception:
        return
    print(f"[CMD MASUK] {cmd}")

    if topic != TOPIC_CONTROL:
        return

    if cmd == "MODE_AUTO":
        system_mode = "AUTO"
        if have_data:
            apply_auto()         
    elif cmd == "MODE_MANUAL":
        enter_manual()
        set_buzzer(False)
    elif cmd == "ON":
        enter_manual()
        set_leds(1, 1, 1)
        led_state = "ALL_ON"
    elif cmd == "OFF":
        enter_manual()
        set_leds(0, 0, 0)
        led_state = "OFF"
        set_buzzer(False)
    elif cmd == "VENT_OPEN":
        enter_manual()
        set_servo_angle(180)
        vent_state = "OPEN_MAX"
    elif cmd == "VENT_CLOSE":
        enter_manual()
        set_servo_angle(0)
        vent_state = "CLOSED"

# JARINGAN
def setup_wifi():
    oled.fill(0)
    oled.text("Koneksi WiFi...", 0, 10)
    oled.show()
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    wlan.connect(WIFI_SSID, WIFI_PASS)
    t0 = time.ticks_ms()
    # Tunggu maksimal 15 detik; kalau gagal, sistem tetap jalan offline
    while not wlan.isconnected() and time.ticks_diff(time.ticks_ms(), t0) < 15000:
        time.sleep(0.5)
    return wlan


def try_connect():
    """Satu kali percobaan konek MQTT. Return client atau None (tidak ada loop tak terbatas)."""
    try:
        print("[MQTT] Menghubungkan...", end="")
        uid = ubinascii.hexlify(machine.unique_id()).decode()
        client = MQTTClient(
            client_id=f"ESP32-{uid}",
            server=BROKER_HOST,
            port=BROKER_PORT,
            user=BROKER_USER,
            password=BROKER_PASS,
            keepalive=60,
            ssl=True,
            ssl_params={"server_hostname": BROKER_HOST}
        )
        client.set_callback(on_mqtt_message)
        client.connect()
        client.subscribe(TOPIC_CONTROL)
        print(" terhubung!")
        return client
    except Exception as err:
        print(f" gagal ({err})")
        return None


def close_mqtt():
    global mqtt_client, mqtt_ok
    try:
        if mqtt_client:
            mqtt_client.disconnect()
    except Exception:
        pass
    mqtt_client = None
    mqtt_ok = False

# KALIBRASI AWAL
def auto_calibrate():
    """Ambil baseline gas saat boot. Threshold dibatasi agar tidak melebihi rentang ADC 12-bit."""
    global gas_baseline, gas_th_sedang, gas_th_buruk
    oled.fill(0)
    oled.text("Kalibrasi Gas...", 0, 10)
    oled.text("Harap Tunggu 3s", 0, 25)
    oled.show()

    total = 0
    for _ in range(6):
        total += gas_adc.read()
        time.sleep(0.5)

    gas_baseline = int(total / 6)
    gas_th_buruk = min(gas_baseline + 1600, 3800)
    gas_th_sedang = min(gas_baseline + 600, 3300)
    if gas_th_buruk - gas_th_sedang < 300: 
        gas_th_sedang = gas_th_buruk - 300
    print(f"[KALIBRASI] Baseline: {gas_baseline} | Sedang: {gas_th_sedang} | Buruk: {gas_th_buruk}")

# PEMBACAAN SENSOR
def read_sensors(now):
    global temp, hum, light_val, air_val, air_stat
    global prev_air_val, early_warning, warn_until, have_data

    try:
        dht_sensor.measure()
        temp = moving_average(buf_temp, dht_sensor.temperature())
        hum = moving_average(buf_hum, dht_sensor.humidity())
    except OSError:
        pass

    light_val = int(moving_average(buf_light, 4095 - ldr_adc.read()))
    air_val = int(moving_average(buf_air, gas_adc.read()))

    # Rate of change
    delta_air = 0 if prev_air_val is None else (air_val - prev_air_val)
    prev_air_val = air_val

    air_stat = update_air_status(air_stat, air_val)

    if delta_air > WARN_DELTA:
        early_warning = True
        warn_until = time.ticks_add(now, WARN_HOLD_MS)
    elif early_warning and time.ticks_diff(now, warn_until) >= 0:
        early_warning = False

    have_data = True

# TAMPILAN OLED
def update_oled_screen():
    oled.fill(0)
    oled.text(f"T:{temp:.1f}C H:{hum:.0f}%", 0, 0)
    oled.text(f"Luminos:{light_val}", 0, 11)
    oled.text(f"Gas:{air_val} {air_stat}", 0, 22)
    oled.text(f"Vent:{servo_angle} deg", 0, 33)
    oled.text(f"M:{system_mode} W:{'Y' if early_warning else 'N'}", 0, 44)
    oled.text(f"MQTT:{'OK' if mqtt_ok else 'OFF'}", 0, 55)
    oled.show()

# PUBLISH TELEMETRI
def publish_telemetry():
    global mqtt_client, mqtt_ok
    if mqtt_client is None or not have_data:
        return
    json_payload = (
        f'{{"temperature":{temp:.1f},'
        f'"humidity":{hum:.0f},'
        f'"light":{light_val},'
        f'"air_quality":{air_val},'
        f'"air_status":"{air_stat}",'
        f'"led":"{led_state}",'
        f'"vent":"{vent_state}",'
        f'"warning":{str(early_warning).lower()},'
        f'"mode":"{system_mode}"}}'
    )
    try:
        mqtt_client.publish(TOPIC_DATA, json_payload.encode())
        print(f"[PUB] {json_payload}")
    except Exception as err:
        print(f"[PUB] gagal ({err})")
        close_mqtt()

# LOOP UTAMA
def run():
    global mqtt_client, mqtt_ok

    set_servo_angle(0)
    wlan = setup_wifi()
    auto_calibrate()

    mqtt_client = try_connect()
    mqtt_ok = mqtt_client is not None

    t0 = time.ticks_ms()
    last_read = time.ticks_add(t0, -READ_MS) 
    last_pub = time.ticks_add(t0, -PUB_MS)
    last_blink = t0
    last_retry = t0
    blink_state = 0

    while True:
        now = time.ticks_ms()
        if mqtt_client is None:
            mqtt_ok = False
            if time.ticks_diff(now, last_retry) >= RETRY_MS:
                last_retry = now
                if not wlan.isconnected():
                    try:
                        wlan.connect(WIFI_SSID, WIFI_PASS)
                    except Exception:
                        pass
                else:
                    mqtt_client = try_connect()
                    mqtt_ok = mqtt_client is not None
        else:
            try:
                mqtt_client.check_msg()
            except Exception as err:
                print(f"[MQTT] putus ({err})")
                close_mqtt()

        check_manual_timeout(now)

        if system_mode == "AUTO" and early_warning and air_stat != "BURUK" and have_data:
            if time.ticks_diff(now, last_blink) >= BLINK_MS:
                last_blink = now
                blink_state = 0 if blink_state else 1
                led_y.value(blink_state)

        if time.ticks_diff(now, last_read) >= READ_MS:
            last_read = now
            read_sensors(now)
            if system_mode == "AUTO":
                apply_auto()
            update_oled_screen()

        if time.ticks_diff(now, last_pub) >= PUB_MS:
            last_pub = now
            publish_telemetry()

        time.sleep(0.05)


if __name__ == "__main__":
    run()