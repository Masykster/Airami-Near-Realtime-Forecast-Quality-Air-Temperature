import time
import network
import ubinascii
import machine
from machine import Pin, ADC, PWM, SoftI2C
import dht
import framebuf
from umqtt.simple import MQTTClient

# KONFIGURASI JARINGAN & BROKER MQTT
WIFI_SSID   = "Wokwi-GUEST"
WIFI_PASS   = ""

BROKER_HOST = "y16d2f91.ala.asia-southeast1.emqxsl.com"
BROKER_PORT = 8883
BROKER_USER = "adamnirvan"
BROKER_PASS = "orichicrist"

DEVICE_NAME   = "esp32-lab-01"
TOPIC_DATA    = f"iot/{DEVICE_NAME}/telemetry".encode()
TOPIC_CONTROL = f"iot/{DEVICE_NAME}/cmd/led".encode()

# KONFIGURASI PIN
PIN_DHT     = 15
PIN_LED_R   = 2    # Merah  (Buruk / Bahaya Karhutla)
PIN_LED_Y   = 4    # Kuning (Sedang / Waspada)
PIN_LED_G   = 5    # Hijau  (Baik / Udara Bersih)
PIN_SMOKE   = 33   # Sensor Partikel Asap PM2.5 (Slide Potentiometer)
PIN_LDR     = 34   # Sensor Cahaya (Visibilitas Kabut Asap)
PIN_GAS     = 35   # Sensor Gas Pembakaran CO2 (MQ-2)
PIN_SERVO   = 18
PIN_BUZZER  = 19
PIN_SCL     = 22
PIN_SDA     = 21

# PARAMETER SISTEM & AMBANG BATAS AQI (Jurnal Azizah et al., 2025)
READ_MS           = 1000     
PUB_MS            = 5000     
RETRY_MS          = 5000     
BLINK_MS          = 250     
FILTER_SIZE       = 3        

AQI_TH_SEDANG     = 51       # AQI 51-100: Moderate
AQI_TH_BURUK      = 101      # AQI >= 101: Unhealthy
HYST_AQI          = 4        # Histeresis AQI

WARN_DELTA_AQI    = 15       # Lonjakan AQI mendadak (>= 15 poin/detik)
WARN_HOLD_MS      = 10000    
TEMP_HOT          = 32.0     
MANUAL_TIMEOUT_MS = 300000   

# STATE SISTEM
system_mode   = "AUTO"
manual_since  = 0
led_state     = "GREEN"
vent_state    = "OPEN_CLEAN(180)"
servo_angle   = -1     

gas_zero_adc  = 0            # Menyimpan titik nol sensor gas Wokwi saat boot
prev_aqi_val  = None 
early_warning = False
warn_until    = 0

# Data sensor (5 Parameter) & hasil klasifikasi
temp       = 0.0
hum        = 0.0
light_val  = 0
pm25_val   = 0.0
co2_val    = 400
env_factor = 1.0
aqi_val    = 0
smoke_stat = "AMAN"
air_stat   = "BAIK"
have_data  = False

# Status koneksi
mqtt_client = None
mqtt_ok     = False

# Buffer moving average untuk kelima parameter
buf_temp, buf_hum, buf_light, buf_smoke, buf_gas = [], [], [], [], []

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

smoke_adc = ADC(Pin(PIN_SMOKE))
smoke_adc.atten(ADC.ATTN_11DB)
smoke_adc.width(ADC.WIDTH_12BIT)

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

# FUNGSI BANTU AKTUATOR & MATEMATIKA
def set_servo_angle(angle):
    """Set sudut servo 0-180. 0 = Ventilasi Tertutup, 180 = Terbuka Penuh."""
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
    if in_max == in_min:
        return out_min
    val = int((x - in_min) * (out_max - out_min) / (in_max - in_min) + out_min)
    return max(min(val, out_max), out_min)

# KALIBRASI TITIK NOL SENSOR GAS WOKWI SAAT BOOT
def calibrate_gas_zero():
    """
    Di Wokwi, wokwi-gas-sensor memiliki nilai ADC default ~2150 saat start.
    Kita jadikan bacaan awal tersebut sebagai titik 400 ppm (udara bersih)
    agar sistem tidak langsung error 'BURUK' saat pertama kali di-run.
    """
    global gas_zero_adc
    oled.fill(0)
    oled.text("Inisialisasi...", 0, 15)
    oled.text("Kalibrasi Sensor", 0, 30)
    oled.show()
    
    total = 0
    for _ in range(4):
        total += gas_adc.read()
        time.sleep(0.2)
    gas_zero_adc = int(total / 4)
    print(f"[BOOT] Titik nol ADC Gas Wokwi dikalibrasi pada: {gas_zero_adc}")

# PERHITUNGAN KLASIFIKASI 5 PARAMETER (PM2.5, CO2, SUHU, HUMIDITY, CAHAYA)
def calculate_aqi_pm25(pm25):
    """1. Sub-Indeks Parameter Asap PM2.5 sesuai Tabel 1 & 5 Jurnal."""
    breakpoints = [
        (0.0, 12.0, 0, 50),        # Good (Baik)
        (12.1, 35.4, 51, 100),     # Moderate (Sedang)
        (35.5, 55.4, 101, 150),    # Unhealthy for Sensitive Groups
        (55.5, 150.4, 151, 200),   # Unhealthy
        (150.5, 250.4, 201, 300),  # Very Unhealthy
        (250.5, 500.0, 301, 500)   # Hazardous
    ]
    c = max(0.0, min(pm25, 500.0))
    for c_low, c_high, i_low, i_high in breakpoints:
        if c <= c_high:
            return int(((i_high - i_low) / (c_high - c_low)) * (c - c_low) + i_low)
    return 500

def calculate_aqi_co2(co2_ppm):
    """
    2. Sub-Indeks Parameter Gas CO2 sesuai Gambar 4 & 7 Jurnal:
    - 400 - 700 ppm  -> AQI 0 - 50 (Baik)
    - 701 - 1200 ppm -> AQI 51 - 100 (Sedang; di jurnal 850 ppm = AQI 75)
    - 1201 - 3000 ppm -> AQI 101 - 300 (Buruk)
    """
    if co2_ppm <= 700:
        return map_value(co2_ppm, 400, 700, 0, 50)
    elif co2_ppm <= 1200:
        return map_value(co2_ppm, 701, 1200, 51, 100)
    else:
        return map_value(co2_ppm, 1201, 3000, 101, 300)

def calculate_multisensor_aqi(pm25, co2_ppm, t, h, light):
    """
    Menggabungkan ke-5 parameter:
    - PM2.5 (Asap) & CO2 (Gas) sebagai indeks polutan dasar
    - Suhu (t), Kelembapan (h), dan Cahaya (light) sebagai pengali risiko Karhutla
    """
    global env_factor
    aqi_pm  = calculate_aqi_pm25(pm25)
    aqi_gas = calculate_aqi_co2(co2_ppm)

    # Jika salah satu polutan ekstrem, gunakan nilai tertinggi; jika normal, gunakan rata-rata berbobot
    if aqi_pm >= 101 or aqi_gas >= 101:
        base_aqi = max(aqi_pm, aqi_gas)
    else:
        base_aqi = (0.6 * aqi_pm) + (0.4 * aqi_gas)

    # Hitung Faktor Lingkungan dari Suhu, Kelembapan, dan Cahaya (Visibilitas)
    factor = 1.0
    
    # Parameter 3: Suhu (Panas ekstrem > 33C meningkatkan risiko Karhutla +10%)
    if t > 33.0:
        factor += 0.10
        
    # Parameter 4: Kelembapan (Udara kering < 45% membuat asap bertahan di udara +10%)
    if h < 45.0:
        factor += 0.10
    elif h > 70.0:
        factor -= 0.05  # Kelembapan tinggi membantu mengendapkan partikel
        
    # Parameter 5: Cahaya/LDR (Visibilitas rendah < 1200 saat ada polutan = Kabut Asap Pekat +15%)
    if light < 1200 and base_aqi > 30:
        factor += 0.15

    env_factor = round(factor, 2)
    final_aqi = int(min(500, max(0, base_aqi * env_factor)))
    return final_aqi

def evaluate_smoke_status(pm25, co2_ppm, light):
    """Menentukan status kepekatan asap dari PM2.5, CO2, dan visibilitas cahaya."""
    if pm25 >= 35.5 or co2_ppm >= 1201 or (pm25 >= 25.0 and light < 1000):
        return "PEKAT"
    elif pm25 >= 12.1 or co2_ppm >= 701:
        return "TIPIS"
    return "AMAN"

def update_air_status(stat, aqi):
    """Klasifikasi status udara (BAIK, SEDANG, BURUK) dengan histeresis."""
    if stat == "BAIK":
        if aqi >= AQI_TH_BURUK:
            return "BURUK"
        if aqi >= AQI_TH_SEDANG:
            return "SEDANG"
        return "BAIK"
    if stat == "SEDANG":
        if aqi >= AQI_TH_BURUK:
            return "BURUK"
        if aqi < AQI_TH_SEDANG - HYST_AQI:
            return "BAIK"
        return "SEDANG"
    if aqi < AQI_TH_BURUK - HYST_AQI:
        if aqi < AQI_TH_SEDANG - HYST_AQI:
            return "BAIK"
        return "SEDANG"
    return "BURUK"

# LOGIKA OTOMATIS MITIGASI KARHUTLA
def apply_auto():
    global led_state, vent_state

    if air_stat == "BURUK" or smoke_stat == "PEKAT":
        set_leds(1, 0, 0)
        led_state = "RED"
        angle = 0  # TUTUP RAPAT ventilasi (0 deg) agar asap tidak masuk
        vent_state = "CLOSED_SMOKE(0)"
        set_buzzer(True, 1500)

    elif early_warning:
        led_r.value(0)
        led_g.value(0)
        led_state = "WARNING_BLINK"
        angle = 30 # Siaga tutup (30 deg) karena lonjakan mendadak
        vent_state = "PRE-CLOSE(30)"
        set_buzzer(True, 2000)

    elif air_stat == "SEDANG" or smoke_stat == "TIPIS":
        set_leds(0, 1, 0)
        led_state = "YELLOW"
        # Semakin tinggi AQI (51 -> 100), sudut servo semakin menutup (150 -> 30 deg)
        closing_offset = map_value(aqi_val, AQI_TH_SEDANG, AQI_TH_BURUK, 30, 150)
        angle = 180 - closing_offset
        vent_state = f"PROP_CLOSE({angle})"
        set_buzzer(False)

    else: # Udara BAIK & AMAN
        set_leds(0, 0, 1)
        led_state = "GREEN"
        angle = 180 # BUKA PENUH (180 deg)
        vent_state = "OPEN_CLEAN(180)"
        set_buzzer(False)

    # Parameter Suhu juga mengontrol servo: jika suhu ruangan panas (>32C) 
    # dan udara luar masih BERSIH, pastikan ventilasi terbuka penuh (180 deg)
    if temp > TEMP_HOT and air_stat == "BAIK" and smoke_stat == "AMAN" and not early_warning:
        angle = 180
        vent_state = "TEMP_OPEN(180)"

    set_servo_angle(angle)

# MODE MANUAL
def enter_manual():
    global system_mode, manual_since
    system_mode = "MANUAL"
    manual_since = time.ticks_ms()

def check_manual_timeout(now):
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
        vent_state = "MANUAL_OPEN(180)"
    elif cmd == "VENT_CLOSE":
        enter_manual()
        set_servo_angle(0)
        vent_state = "MANUAL_CLOSED(0)"

# JARINGAN
def setup_wifi():
    oled.fill(0)
    oled.text("Koneksi WiFi...", 0, 10)
    oled.show()
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    wlan.connect(WIFI_SSID, WIFI_PASS)
    t0 = time.ticks_ms()
    while not wlan.isconnected() and time.ticks_diff(time.ticks_ms(), t0) < 15000:
        time.sleep(0.5)
    return wlan

def try_connect():
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

# PEMBACAAN 5 SENSOR & PERHITUNGAN KLASIFIKASI
def read_sensors(now):
    global temp, hum, light_val, pm25_val, co2_val, aqi_val, smoke_stat, air_stat
    global prev_aqi_val, early_warning, warn_until, have_data

    # 1 & 2. Baca Suhu dan Kelembapan (DHT22)
    try:
        dht_sensor.measure()
        temp = moving_average(buf_temp, dht_sensor.temperature())
        hum = moving_average(buf_hum, dht_sensor.humidity())
    except OSError:
        pass

    # 3, 4, 5. Baca Cahaya (LDR), Asap PM2.5 (Potensiometer), dan Gas (MQ-2)
    raw_light = moving_average(buf_light, 4095 - ldr_adc.read())
    raw_smoke = moving_average(buf_smoke, smoke_adc.read())
    raw_gas   = moving_average(buf_gas, gas_adc.read())

    light_val = int(raw_light)
    pm25_val  = round((raw_smoke / 4095.0) * 300.0, 1)

    # Hitung kenaikan gas di atas titik nol awal (gas_zero_adc)
    # Jika slider gas digeser naik melebihi posisi awal, CO2 naik dari 400 hingga 3000 ppm
    if raw_gas <= gas_zero_adc:
        co2_val = 400
    else:
        span = max(1, 4095 - gas_zero_adc)
        co2_val = int(400 + ((raw_gas - gas_zero_adc) / span) * 2600)

    # Hitung AQI Gabungan dari 5 Parameter (PM2.5, CO2, Temp, Hum, Light)
    aqi_val    = calculate_multisensor_aqi(pm25_val, co2_val, temp, hum, light_val)
    smoke_stat = evaluate_smoke_status(pm25_val, co2_val, light_val)
    air_stat   = update_air_status(air_stat, aqi_val)

    # Deteksi lonjakan AQI mendadak (Early Warning)
    delta_aqi = 0 if prev_aqi_val is None else (aqi_val - prev_aqi_val)
    prev_aqi_val = aqi_val

    if delta_aqi >= WARN_DELTA_AQI:
        early_warning = True
        warn_until = time.ticks_add(now, WARN_HOLD_MS)
    elif early_warning and time.ticks_diff(now, warn_until) >= 0:
        early_warning = False

    have_data = True

# TAMPILAN OLED
def update_oled_screen():
    oled.fill(0)
    oled.text(f"T:{temp:.1f}C H:{hum:.0f}%", 0, 0)
    oled.text(f"PM25:{pm25_val:.0f} CO2:{co2_val}", 0, 11)
    oled.text(f"Lux:{light_val} Env:{env_factor}x", 0, 22)
    oled.text(f"AQI:{aqi_val} ({air_stat})", 0, 33)
    oled.text(f"Asap:{smoke_stat} V:{servo_angle}", 0, 44)
    oled.text(f"M:{system_mode} MQTT:{'OK' if mqtt_ok else 'X'}", 0, 55)
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
        f'"pm25":{pm25_val},'
        f'"co2_ppm":{co2_val},'
        f'"env_multiplier":{env_factor},'
        f'"aqi":{aqi_val},'
        f'"smoke_status":"{smoke_stat}",'
        f'"air_status":"{air_stat}",'
        f'"led":"{led_state}",'
        f'"vent":"{vent_state}",'
        f'"servo_angle":{servo_angle},'
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

    # Saat baru menyala, ventilasi terbuka (180 deg) dan LED Hijau menyala
    set_servo_angle(180)
    set_leds(0, 0, 1)
    
    wlan = setup_wifi()
    calibrate_gas_zero()

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