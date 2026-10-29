# Airami: Sistem IoT Pencegah Paparan Polusi Udara Dalam Ruang

![ESP32](https://img.shields.io/badge/ESP32-DevKit_C_V4-E7352C?style=flat-square&logo=espressif&logoColor=white)
![MicroPython](https://img.shields.io/badge/MicroPython-Wokwi-2B5B84?style=flat-square&logo=python&logoColor=white)
![MQTT](https://img.shields.io/badge/MQTT-TLS_8883-660066?style=flat-square&logo=mqtt&logoColor=white)
![Broker](https://img.shields.io/badge/Broker-EMQX-00B173?style=flat-square)
![Status](https://img.shields.io/badge/status-simulasi_edukasi_karhutla-lightgrey?style=flat-square)

Simulasi **Wokwi** berbasis **ESP32 DevKit C V4 (MicroPython)** untuk memantau kualitas udara secara *real-time* dengan studi kasus mitigasi **Kebakaran Hutan dan Lahan (Karhutla)**. Sistem ini memantau lima parameter lingkungan (suhu, kelembapan, cahaya, partikulat asap $\text{PM}_{2.5}$, dan gas $\text{CO}_2$), menampilkan data secara live di OLED, mengirim telemetri JSON via MQTT ke EMQX Cloud, serta mengendalikan aktuator otomatis berupa LED, *buzzer*, dan **ventilasi servo proporsional terbalik** untuk memblokir infiltrasi asap luar ruangan.

> [!NOTE]
> Proyek ini adalah bagian dari sistem *edge-AIoT* Airami yang terintegrasi dengan dasbor analitik Google Colab (*K-Means Clustering* & *Z-Score Anomaly*) serta kendali dua arah via Bot Telegram.

## Fitur Utama

- **Pemantauan Multi-Sensor (5 Parameter):** Membaca suhu/kelembapan (DHT22), visibilitas cahaya (LDR), partikulat asap $\text{PM}_{2.5}$ (potensiometer geser), dan gas pembakaran $\text{CO}_2$ (MQ-2).
- **Filter Moving Average:** Penghalusan sinyal 3 sampel untuk meredam derau (*noise*) sensor.
- **Kalkulasi EPA AQI & Faktor Lingkungan:** Konversi konsentrasi polutan ke indeks AQI standar US EPA yang dikalikan dengan faktor pengali termal dan kekeringan udara (*environmental multiplier*).
- **Logika Ventilasi Proporsional Terbalik (*Reverse Proportional Control*):** Servo ventilasi terbuka penuh ($180^\circ$) saat udara bersih, menyempit secara proporsional ($150^\circ \rightarrow 30^\circ$) saat udara *SEDANG*, bersiaga ($30^\circ$) saat *Early Warning*, dan tertutup rapat ($0^\circ$) saat udara *BURUK* (asap pekat Karhutla).
- **Deteksi Lonjakan Cepat (*Rate-of-Change Early Warning*):** Memicu peringatan dini akustik dan visual apabila terjadi kenaikan AQI mendadak ($\Delta\text{AQI} \ge 15\text{ poin/detik}$).
- **Mode AUTO & MANUAL:** Logika berprioritas otomatis di sisi *edge* dengan fitur *auto-timeout* 5 menit pada mode manual demi keamanan penghuni.
- **Integrasi Cloud & AI (Google Colab):** Visualisasi 4 panel grafik interaktif (Plotly), *K-Means Clustering* ($k=3$), dan deteksi anomali *Z-Score*.
- **Bot Telegram Dua Arah:** Notifikasi darurat otomatis dengan *cooldown* 60 detik serta perintah kendali jarak jauh (`/status`, `/vent_open`, `/vent_close`, `/auto`).

## Komponen & Wiring

| Komponen | Pin Modul | Pin ESP32 | GPIO | Catatan |
|---|---|---|---|---|
| DHT22 (suhu & kelembapan) | VCC / GND / SDA | 3V3 / GND / D15 | 15 | Data 1-wire |
| OLED SSD1306 128x64 | VCC / GND / SCL / SDA | 3V3 / GND / D22 / D21 | 22, 21 | I2C alamat `0x3C` |
| Slide Potentiometer (simulasi asap $\text{PM}_{2.5}$) | VCC / GND / SIG | 3V3 / GND / D33 | 33 | ADC1, input-only |
| LDR (photoresistor / cahaya) | VCC / GND / AO | 3V3 / GND / D34 | 34 | ADC1, input-only |
| Sensor gas MQ-2 ($\text{CO}_2$) | VCC / GND / AOUT | 3V3 / GND / D35 | 35 | ADC1, input-only |
| LED merah + resistor 220 Ω | A → GPIO, C → resistor → GND | D2 | 2 | Status BURUK / Asap Pekat |
| LED kuning + resistor 220 Ω | A → GPIO, C → resistor → GND | D4 | 4 | Status SEDANG / Early Warning (kedip) |
| LED hijau + resistor 220 Ω | A → GPIO, C → resistor → GND | D5 | 5 | Status BAIK / Udara Bersih |
| Servo (ventilasi proporsional) | V+ / GND / PWM | 5V / GND / D18 | 18 | PWM 50 Hz: 0° tertutup rapat, 180° terbuka penuh |
| Buzzer | 1 (GND) / 2 (sinyal) | GND / D19 | 19 | Digerakkan PWM (1500 Hz & 2000 Hz) |

> [!TIP]
> Wiring lengkap tersedia di berkas [`diagram.json`](diagram.json). Pastikan pin analog menggunakan kanal `ADC1` (`GPIO 33`, `34`, `35`) agar dapat dibaca bersamaan saat modul WiFi aktif.

## Arsitektur Sistem

```mermaid
flowchart LR
    S["DHT22, LDR, Potensiometer PM2.5, Sensor Gas MQ-2"] --> F["Moving average filter (3 sampel)"]
    F --> O["OLED SSD1306"]
    F --> K["Multi-Sensor EPA AQI + Histeresis"]
    CAL["Kalibrasi titik nol gas saat boot"] --> K
    F --> P["Spike Early Warning (Delta AQI)"]
    K --> A["Logika AUTO berprioritas (Reversed Servo)"]
    P --> A
    F --> T["Override Suhu (>32°C & Udara Bersih)"]
    T --> A
    A --> L["LED Merah / Kuning / Hijau"]
    A --> Z["Buzzer Alarm"]
    A --> V["Servo Ventilasi (0° - 180° Proporsional)"]
    F --> M["WiFi + MQTT TLS (Port 8883)"]
    M <--> B[("Broker EMQX Cloud")]
    B -- Telemetri JSON --> C["Google Colab (Plotly, K-Means, Z-Score)"]
    B -- Notifikasi & Kendali --> TG["Bot Telegram (API)"]
