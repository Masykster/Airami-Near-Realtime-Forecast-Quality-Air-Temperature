import time
import network
import ubinascii
import machine
from machine import Pin, ADC, SoftI2C
import dht
import framebuf
from umqtt.simple import MQTTClient

# Konfigurasi Jaringan & Broker MQTT
WIFI_SSID = "Wokwi-GUEST"
WIFI_PASS = ""

BROKER_HOST = "t2cce229.ala.asia-southeast1.emqxsl.com" # Ganti sesuai URL EMQX kamu
BROKER_PORT = 8883
BROKER_USER = "masykur2006"                             # Ganti sesuai username kamu
BROKER_PASS = "12345678"                                # Ganti sesuai password kamu

DEVICE_NAME   = "esp32-lab-01"
TOPIC_DATA    = f"iot/{DEVICE_NAME}/telemetry".encode()
TOPIC_CONTROL = f"iot/{DEVICE_NAME}/cmd/led".encode()

# Konfigurasi Pin Perangkat Keras
PIN_DHT = 15
PIN_LED = 2
PIN_LDR = 34
PIN_GAS = 35  # --- BARU: Pin ADC1 untuk sensor kualitas udara (MQ) ---
PIN_SCL = 22
PIN_SDA = 21


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


# Inisialisasi Sensor, LED, dan Layar OLED
dht_sensor = dht.DHT22(Pin(PIN_DHT))
led_out = Pin(PIN_LED, Pin.OUT, value=0)

ldr_adc = ADC(Pin(PIN_LDR))
ldr_adc.atten(ADC.ATTN_11DB)
ldr_adc.width(ADC.WIDTH_12BIT)

# --- BARU: Inisialisasi ADC Sensor Kualitas Udara ---
gas_adc = ADC(Pin(PIN_GAS))
gas_adc.atten(ADC.ATTN_11DB)
gas_adc.width(ADC.WIDTH_12BIT)

i2c_bus = SoftI2C(scl=Pin(PIN_SCL), sda=Pin(PIN_SDA), freq=400000)
try:
    oled = OledSSD1306(128, 64, i2c_bus, addr=0x3C)
except OSError:
    print("Gagal menginisialisasi layar SSD1306")
    while True:
        time.sleep(1)


def get_air_status(adc_val):
    """Menentukan kategori kualitas udara dari nilai ADC (0 - 4095)."""
    if adc_val < 1200:
        return "BAIK"
    elif adc_val < 2500:
        return "SEDANG"
    return "BURUK"


def on_mqtt_message(topic, msg_bytes):
    """Menangani pesan perintah kendali LED dari topik MQTT."""
    command = msg_bytes.decode("utf-8").strip().upper()
    if topic == TOPIC_CONTROL:
        if command == "ON":
            led_out.value(1)
            print("-> LED ON (remote)")
        elif command == "OFF":
            led_out.value(0)
            print("-> LED OFF (remote)")


def setup_wifi():
    """Menyambungkan ESP32 ke jaringan Wi-Fi dan menampilkan status di OLED."""
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
    """Membuat koneksi TLS ke broker MQTT dan berlangganan topik."""
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


def update_oled_screen(temp, hum, light, air_val, air_stat, led_state, ip_addr):
    """Memperbarui tampilan layar OLED 128x64 dengan data kualitas udara."""
    oled.fill(0)
    oled.text(f"Suhu  : {temp:.1f} C", 0, 0)
    oled.text(f"Hum   : {hum:.0f} %", 0, 10)
    oled.text(f"Cahaya: {light}", 0, 20)
    oled.text(f"Udara : {air_val}({air_stat})", 0, 30)
    oled.text(f"LED   : {led_state}", 0, 40)
    oled.text(f"IP: {ip_addr}", 0, 50)
    oled.show()


def run():
    wlan = setup_wifi()
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
                temp = dht_sensor.temperature()
                hum = dht_sensor.humidity()
            except OSError:
                continue

            light_val = 4095 - ldr_adc.read()
            air_val   = gas_adc.read()  # Semakin tinggi nilai = semakin pekat gas/polusi
            air_stat  = get_air_status(air_val)

            # Rule lokal: LED menyala jika suhu di atas 30 C ATAU kualitas udara BURUK
            if temp > 30.0 or air_val >= 2500:
                led_out.value(1)
                led_state = "ON"
            else:
                led_out.value(0)
                led_state = "OFF"

            ip_address = wlan.ifconfig()[0]
            update_oled_screen(temp, hum, light_val, air_val, air_stat, led_state, ip_address)

            json_payload = (
                f'{{"temperature":{temp:.1f},'
                f'"humidity":{hum:.0f},'
                f'"light":{light_val},'
                f'"air_quality":{air_val},'
                f'"air_status":"{air_stat}",'
                f'"led":"{led_state}"}}'
            )

            try:
                mqtt_client.publish(TOPIC_DATA, json_payload.encode())
                print(f"[PUB] {json_payload}")
            except Exception:
                mqtt_client = connect_broker()

        time.sleep(0.1)


if __name__ == "__main__":
    run()
