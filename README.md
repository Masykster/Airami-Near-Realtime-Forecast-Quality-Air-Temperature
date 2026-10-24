# ESP32 Air Quality & Temperature Detector

Simulasi **Wokwi** berbasis **ESP32 DevKit C V4 (MicroPython)** untuk memantau suhu,
kelembapan, intensitas cahaya, dan kualitas udara secara real-time. Data ditampilkan
di OLED 128x64 dan dikirim ke broker MQTT (EMQX) melalui koneksi TLS, sekaligus
menerima perintah kendali LED dari jarak jauh.

## Fitur

- 📊 Pembacaan 4 parameter: suhu, kelembapan, cahaya, dan kadar gas/polusi udara
- 🖥️ Tampilan live di OLED SSD1306 128x64 (I2C)
- ☁️ Publish telemetri JSON ke MQTT over TLS (port 8883) setiap 5 detik
- 🎛️ Kendali LED remote via topik perintah MQTT (`ON` / `OFF`)
- 🔁 Auto-reconnect WiFi + broker MQTT
- 🧠 Rule lokal: LED menyala otomatis jika suhu > 30 °C **atau** kualitas udara buruk

## Komponen & Wiring

| Komponen | Pin Modul | Pin ESP32 | GPIO |
|---|---|---|---|
| DHT22 (suhu & kelembapan) | VCC / GND / SDA | 3V3 / GND / D15 | 15 |
| OLED SSD1306 128x64 | VCC / GND / SCL / SDA | 3V3 / GND / D22 / D21 | 22, 21 |
| LDR (photoresistor) | VCC / GND / AO | 3V3 / GND / D34 | 34 (ADC1) |
| Gas sensor (MQ) | VCC / GND / AOUT | 3V3 / GND / D35 | 35 (ADC1) |
| LED merah + resistor 220 Ω | A / C → R → GND | D2 | 2 |

> Wiring lengkap dapat dilihat di [`diagram.json`](diagram.json).

## Skema MQTT

| Keperluan | Topik | Arah | Payload |
|---|---|---|---|
| Telemetri | `iot/esp32-lab-01/telemetry` | ESP32 → broker | JSON |
| Kendali LED | `iot/esp32-lab-01/cmd/led` | broker → ESP32 | `ON` / `OFF` |

Contoh payload telemetri:

```json
{
  "temperature": 28.5,
  "humidity": 65,
  "light": 3120,
  "air_quality": 980,
  "air_status": "BAIK",
  "led": "OFF"
}
```

Keterangan field:

- `light` — nilai terbalik dari ADC LDR (`4095 - ADC`), makin besar = makin terang
- `air_quality` — nilai mentah ADC sensor gas, makin besar = makin pekat polusi

## Ambang Batas Kualitas Udara

| Rentang ADC | Status |
|---|---|
| `< 1200` | `BAIK` |
| `1200 – 2499` | `SEDANG` |
| `>= 2500` | `BURUK` |

LED lokal menyala jika `suhu > 30.0 °C` **atau** `air_quality >= 2500`.

## Cara Menjalankan

1. Buka [Wokwi](https://wokwi.com) → **New Project** → **ESP32 (MicroPython)**.
2. Salin isi `diagram.json` ke tab `diagram.json`.
3. Salin isi `main.py` ke tab `main.py`.
4. Sesuaikan konfigurasi broker di bagian atas `main.py`:

   ```python
   WIFI_SSID   = "Wokwi-GUEST"   # jaringan virtual Wokwi
   WIFI_PASS   = ""
   BROKER_HOST = "<host broker EMQX kamu>"
   BROKER_PORT = 8883
   BROKER_USER = "<username>"
   BROKER_PASS = "<password>"
   DEVICE_NAME = "esp32-lab-01"
   ```

5. Klik **▶ Start Simulation**. OLED akan menampilkan status koneksi lalu data sensor.
6. Uji kendali remote: publish `ON` atau `OFF` ke topik `iot/esp32-lab-01/cmd/led`.
7. Pantau output serial / payload published di Serial Monitor.

> `Wokwi-GUEST` hanya tersedia di simulator Wokwi. Untuk board fisik, ganti dengan
> SSID dan password WiFi asli.

## Struktur Proyek

```
.
├── diagram.json   # Skema rangkaian Wokwi (komponen + koneksi)
├── main.py        # Firmware MicroPython (sensor, OLED, MQTT)
└── README.md
```

`main.py` memakai modul `umqtt.simple` yang sudah tersedia di build MicroPython Wokwi.
Jika muncul `ImportError: no module named 'umqtt'`, tambahkan `umqtt/simple.py` dari
[micropython-lib](https://github.com/micropython/micropython-lib) ke proyek Wokwi.

## ⚠️ Catatan Keamanan

Kredensial broker saat ini **hardcoded** di `main.py` dan ikut ter-push ke repositori
publik. Sebaiknya:

- Pindahkan kredensial ke file `config.py` yang masuk `.gitignore`, lalu import di `main.py`.
- Ganti (rotate) password broker EMQX karena nilai lama sudah terekspos.
- Gunakan kredensial khusus perangkat, bukan akun utama broker.
