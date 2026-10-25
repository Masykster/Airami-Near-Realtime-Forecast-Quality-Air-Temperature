import time
import network
import ubinascii
import machine
from machine import Pin, ADC, PWM, SoftI2C
import dht
import framebuf
from umqtt.simple import MQTTClient

# Konfigurasi Jaringan & Broker MQTT
WIFI_SSID = "Wokwi-GUEST"
WIFI_PASS = ""

BROKER_HOST = "t2cce229.ala.asia-southeast1.emqxsl.com"
BROKER_PORT = 8883
BROKER_USER = "masykur2006"
BROKER_PASS = "12345678"

DEVICE_NAME   = "esp32-lab-01"
TOPIC_DATA    = f"iot/{DEVICE_NAME}/telemetry".encode()
TOPIC_CONTROL = f"iot/{DEVICE_NAME}/cmd/led".encode()

# Konfigurasi Pin Perangkat Keras
PIN_DHT    = 15
PIN_LED    = 2
PIN_LDR    = 34
PIN_GAS    = 35
PIN_SERVO  = 18  # Motor Servo (Ventilasi Otomatis)
PIN_BUZZER = 19  # Buzzer PWM (Alarm Bahaya)
PIN_SCL    = 22
PIN_SDA    = 21

# State Sistem (AUTO / MANUAL)
system_mode = "AUTO"
led_state   = "OFF"
vent_state  = "CLOSED"

# Buffer Moving Average (Edge Computing Filter - 3 sampel terakhir)
buf_temp, buf_hum, buf_light, buf_air = [], [], [], []
FILTER_SIZE = 3


class OledSSD1306(framebuf.FrameBuffer):
    """Penggerak ringkas OLED 128x64 I2C menggunakan modul framebuf bawaan."""
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


# Inisialisasi Sensor & Aktuator
dht_sensor = dht.DHT22(Pin(PIN_DHT))
led_out = Pin(PIN_LED, Pin.OUT, value=0)

ldr_adc = ADC(Pin(PIN_LDR))
ldr_adc.atten(ADC.ATTN_11DB)
ldr_adc.width(ADC.WIDTH_12BIT)

gas_adc = ADC(Pin(PIN_GAS))
gas_adc.atten(ADC.ATTN_11DB)
gas_adc.width(ADC.WIDTH_12BIT)

# Setup PWM Servo (50Hz) & Buzzer
servo_pwm = PWM(Pin(PIN_SERVO), freq=50)
buzzer_pwm = PWM(Pin(PIN_BUZZER), freq=1000, duty=0)

i2c_bus = SoftI2C(scl=Pin(PIN_SCL), sda=Pin(PIN_SDA), freq=400000)
oled = OledSSD1306(128, 64, i2c_bus, addr=0x3C)


def set_servo_angle(angle):
    """Mengatur sudut motor servo (0 - 180 derajat)."""
    duty = int(26 + (angle / 180.0) * 102)
    servo_pwm.duty(duty)


def set_buzzer(active, freq=1200):
    """Mengaktifkan atau mematikan alarm buzzer."""
    if active:
        buzzer_pwm.freq(freq)
        buzzer_pwm.duty(512)
    else:
        buzzer_pwm.duty(0)


def moving_average(buf, new_val):
    """Menghitung rata-rata bergerak untuk menghaluskan fluktuasi sensor."""
    buf.append(new_val)
    if len(buf) > FILTER_SIZE:
        buf.pop(0)
    return sum(buf) / len(buf)


def get_air_status(adc_val):
    if adc_val < 1200:
        return "BAIK"
    elif adc_val < 2500:
        return "SEDANG"
    return "BURUK"


def on_mqtt_message(topic, msg_bytes):
    """Menangani perintah kontrol dua arah dari MQTT / Bot Telegram."""
    global system_mode, led_state, vent_state
    cmd = msg_bytes.decode("utf-8").strip().upper()
    print(f"[CMD MASUK] {cmd}")

    if topic == TOPIC_CONTROL:
        if cmd == "MODE_AUTO":
            system_mode = "AUTO"
        elif cmd == "MODE_MANUAL":
            system_mode = "MANUAL"
            set_buzzer(False)
        elif cmd == "ON":
            system_mode = "MANUAL"
            led_out.value(1)
            led_state = "ON"
        elif cmd == "OFF":
            system_mode = "MANUAL"
            led_out.value(0)
            led_state = "OFF"
            set_buzzer(False)
        elif cmd == "VENT_OPEN":
            system_mode = "MANUAL"
            set_servo_angle(90)
            vent_state = "OPEN"
        elif cmd == "VENT_CLOSE":
            system_mode = "MANUAL"
            set_servo_angle(0)
            vent_state = "CLOSED"


def setup_wifi():
    oled.fill(0)
    oled.text("Menghubungkan", 0, 0)
    oled.text("WiFi...", 0, 10)
    oled.show()

    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    wlan.connect(WIFI_SSID, WIFI_PASS)
    while not wlan.isconnected():
        time.sleep(0.5)
    return wlan


def connect_broker():
    while True:
        try:
            print("[MQTT] Menghubungkan...", end="")
            uid = ubinascii.hexlify(machine.unique_id()).decode()
            mqtt = MQTTClient(
                client_id=f"ESP32-{uid}",
                server=BROKER_HOST,
                port=BROKER_PORT,
                user=BROKER_USER,
                password=BROKER_PASS,
                keepalive=60,
                ssl=True,
                ssl_params={"server_hostname": BROKER_HOST}
            )
            mqtt.set_callback(on_mqtt_message)
            mqtt.connect()
            print(" terhubung!")
            mqtt.subscribe(TOPIC_CONTROL)
            return mqtt
        except Exception as err:
            print(f" gagal ({err}), coba lagi 5 detik...")
            time.sleep(5)


def update_oled_screen(temp, hum, light, air_val, air_stat):
    oled.fill(0)
    oled.text(f"T:{temp:.1f}C H:{hum:.0f}%", 0, 0)
    oled.text(f"Cahaya: {light}", 0, 11)
    oled.text(f"Udara : {air_val}({air_stat})", 0, 22)
    oled.text(f"LED:{led_state} V:{vent_state}", 0, 33)
    oled.text("-----------------", 0, 44)
    oled.text(f"Mode: {system_mode}", 0, 54)
    oled.show()


def run():
    global led_state, vent_state
    set_servo_angle(0)
    setup_wifi()
    mqtt_client = connect_broker()

    interval_ms = 5000
    last_send = 0

    while True:
        try:
            mqtt_client.check_msg()
        except Exception:
            mqtt_client = connect_broker()

        current_time = time.ticks_ms()
        if time.ticks_diff(current_time, last_send) >= interval_ms:
            last_send = current_time

            try:
                dht_sensor.measure()
                raw_t = dht_sensor.temperature()
                raw_h = dht_sensor.humidity()
            except OSError:
                continue

            raw_l = 4095 - ldr_adc.read()
            raw_a = gas_adc.read()

            # Edge Computing: Haluskan pembacaan dengan Moving Average
            temp      = moving_average(buf_temp, raw_t)
            hum       = moving_average(buf_hum, raw_h)
            light_val = int(moving_average(buf_light, raw_l))
            air_val   = int(moving_average(buf_air, raw_a))
            air_stat  = get_air_status(air_val)

            # Logika Otomatis hanya berjalan jika dalam mode AUTO
            if system_mode == "AUTO":
                if temp > 30.0 or air_val >= 2500:
                    led_out.value(1)
                    led_state = "ON"
                    set_buzzer(True, freq=1500 if air_val >= 2500 else 900)
                else:
                    led_out.value(0)
                    led_state = "OFF"
                    set_buzzer(False)

                # Buka ventilasi (Servo 90°) jika udara BURUK atau suhu > 32°C
                if air_val >= 2500 or temp > 32.0:
                    set_servo_angle(90)
                    vent_state = "OPEN"
                else:
                    set_servo_angle(0)
                    vent_state = "CLOSED"

            update_oled_screen(temp, hum, light_val, air_val, air_stat)

            json_payload = (
                f'{{"temperature":{temp:.1f},'
                f'"humidity":{hum:.0f},'
                f'"light":{light_val},'
                f'"air_quality":{air_val},'
                f'"air_status":"{air_stat}",'
                f'"led":"{led_state}",'
                f'"vent":"{vent_state}",'
                f'"mode":"{system_mode}"}}'
            )

            try:
                mqtt_client.publish(TOPIC_DATA, json_payload.encode())
                print(f"[PUB] {json_payload}")
            except Exception:
                mqtt_client = connect_broker()

        time.sleep(0.1)


if __name__ == "__main__":
    run()
