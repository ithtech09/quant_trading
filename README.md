# AI Swing Trading Signal Bot

Bot pemindai sinyal swing trading (2-3 hari) untuk saham Indonesia, saham
global, dan kripto — analisis teknikal otomatis + laporan email.

## ⚠️ Penting: rotasi kredensial lama

Kode versi sebelumnya menyimpan **App Password Gmail** dan **API key
NewsAPI** dalam bentuk teks biasa di source code. Karena keduanya sempat
terkirim di percakapan ini, segera:

1. Buka [Google Account → Security → App Passwords](https://myaccount.google.com/apppasswords),
   hapus App Password lama, buat yang baru.
2. Buka dashboard NewsAPI, generate ulang API key.

Di kode versi baru ini, **tidak ada kredensial yang hardcode** — semua
dibaca dari file `.env`.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
# lalu edit .env, isi SENDER_EMAIL, EMAIL_APP_PASSWORD, RECEIVER_EMAILS
python trading_agent.py
```

`NEWS_API_KEY` boleh dikosongkan — bot otomatis melewati fitur berita
kalau tidak diisi, sentimen default jadi "NETRAL".

## Apa yang berubah dari kode sebelumnya

1. **Kredensial dipindah ke `.env`** — tidak ada lagi email/password/API key
   hardcode di source code.
2. **AI DuckDuckGo dibuat opsional (bukan lagi dependensi wajib)** — inilah
   penyebab error "belum koneksi": fitur chat gratis DuckDuckGo itu tidak
   resmi (reverse-engineered, tanpa API key resmi) sehingga bisa berhenti
   berfungsi sewaktu-waktu. Sekarang:
   - Import otomatis pakai package baru `ddgs` (nama lama `duckduckgo_search`
     sudah deprecated), dengan fallback ke nama lama kalau belum di-upgrade.
   - Setiap panggilan AI dibungkus retry + timeout.
   - Kalau tetap gagal (atau library-nya tidak terpasang), sistem otomatis
     pakai **ringkasan cadangan berbasis teknikal murni** — bukan AI — jadi
     laporan & Top-3 rekomendasi tetap terkirim lengkap.
3. **Mata uang otomatis sesuai instrumen** — saham Indonesia (`.JK`) tampil
   dalam Rupiah (format Indonesia, contoh `Rp9.850`), saham global & kripto
   tampil dalam Dolar AS (presisi menyesuaikan besaran nilai, karena kripto
   receh seperti PEPE butuh sampai 8 desimal).
4. **Tabel laporan dipisah per jenis instrumen** — Saham Indonesia, Saham
   Global, dan Kripto masing-masing punya tabel sendiri (tabel yang datanya
   kosong otomatis disembunyikan).
5. **Top-3 rekomendasi beli** kini menampilkan titik **Buy**, **Take Profit
   (TP)**, dan **Stop Loss (SL)** — sebelumnya cuma TP & SL.

## Menambah aset yang dipantau

Edit `Config` di `trading_agent.py`:

```python
STOCK_ID_LIST: list = ["BBCA.JK", "TLKM.JK", "ASII.JK"]
STOCK_US_LIST: list = ["AAPL", "MSFT"]   # kosong secara default
CRYPTO_LIST: list = ["BTC-USD", "ETH-USD", ...]
```

## Menjalankan otomatis (cron / scheduler)

Bot ini sekali jalan (bukan loop terus-menerus) — cocok dijadwalkan lewat
`cron` (Linux/Mac) atau Task Scheduler (Windows), misal setiap jam 07:00
dan 15:00 WIB.

## Disclaimer

Sinyal yang dihasilkan bersifat otomatis dari indikator teknikal, bukan
nasihat keuangan. Selalu lakukan riset mandiri (DYOR) dan gunakan
manajemen risiko sebelum bertransaksi.
