"""
AI Swing Trading Signal Bot
============================

Bot pemindai sinyal swing trading (horizon 2-3 hari) untuk saham Indonesia,
saham global, dan kripto. Mengambil data harga dari Yahoo Finance, menghitung
indikator teknikal, menghasilkan sinyal BUY/SELL/HOLD lengkap dengan titik
Buy, Stop Loss (SL), dan Take Profit (TP), lalu mengirim laporan HTML ke email.

Fitur utama:
    - Klasifikasi otomatis instrumen (Saham ID / Saham US / Kripto) sehingga
      mata uang di laporan otomatis benar (Rp vs $).
    - Tabel laporan terpisah per jenis instrumen.
    - Top-N rekomendasi BUY terbaik (berdasarkan skor teknikal) dengan
      titik Buy, TP, dan SL.
    - Ringkasan naratif dari "AI gratis" DuckDuckGo (opsional). Karena layanan
      ini tidak resmi dan bisa putus sewaktu-waktu, semua pemanggilannya
      dibungkus retry + fallback berbasis aturan, sehingga laporan tetap utuh
      walau AI-nya sedang tidak bisa dihubungi.

Konfigurasi (kredensial) dibaca dari environment variable / file .env,
BUKAN dari hardcode di source code. Lihat file `.env.example`.

Cara jalan:
    pip install -r requirements.txt
    cp .env.example .env   # lalu isi kredensialnya
    python trading_agent.py
"""

from __future__ import annotations

import json
import logging
import os
import smtplib
import time
from dataclasses import dataclass, field
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from enum import Enum
from typing import Dict, List, Optional, Tuple

import pandas as pd
import requests
import ta
import yfinance as yf

# python-dotenv sifatnya opsional: kalau tidak terpasang, kita tetap jalan
# dan mengandalkan environment variable yang sudah diset di sistem/CI.
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

# "AI gratis" via DuckDuckGo bersifat OPSIONAL dan tidak resmi.
# Package lama `duckduckgo_search` sudah di-rename menjadi `ddgs`.
# Kita coba yang baru dulu, baru fallback ke yang lama kalau belum di-upgrade.
try:
    from ddgs import DDGS

    _DDGS_AVAILABLE = True
except ImportError:
    try:
        from duckduckgo_search import DDGS  # nama lama, sudah deprecated

        _DDGS_AVAILABLE = True
    except ImportError:
        _DDGS_AVAILABLE = False


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("trading_agent")


# ======================================================================
# 1. KLASIFIKASI INSTRUMEN & FORMAT MATA UANG
# ======================================================================

class AssetType(str, Enum):
    """Kategori instrumen. Menentukan mata uang & tabel mana di laporan."""

    IDX_STOCK = "SAHAM_ID"
    US_STOCK = "SAHAM_US"
    CRYPTO = "KRIPTO"


def classify_asset(symbol: str) -> AssetType:
    """Klasifikasikan ticker Yahoo Finance ke kategori instrumen.

    Mengikuti konvensi penulisan ticker di Yahoo Finance:
        - Diakhiri ".JK"   -> Saham Indonesia (IDX)
        - Diakhiri "-USD"  -> Kripto (BTC-USD, ETH-USD, dst.)
        - Selain itu       -> Saham global/US (AAPL, MSFT, dst.)

    Args:
        symbol: Ticker aset, misal "BBCA.JK", "BTC-USD", "AAPL".

    Returns:
        AssetType yang sesuai.
    """
    upper_symbol = symbol.upper()
    if upper_symbol.endswith(".JK"):
        return AssetType.IDX_STOCK
    if upper_symbol.endswith("-USD"):
        return AssetType.CRYPTO
    return AssetType.US_STOCK


def format_currency(value: float, asset_type) -> str:
    """Format angka harga sesuai mata uang instrumen.

    - Saham Indonesia -> Rupiah, dibulatkan (contoh: Rp9.850)
    - Saham US & Kripto -> Dolar AS, presisi menyesuaikan besaran nilai,
      karena kripto receh seperti PEPE bisa senilai $0.00000123 sehingga
      tidak bisa dibulatkan 2 desimal seperti saham.

    Args:
        value: Nilai harga dalam satuan aslinya (IDR untuk saham ID, USD
            untuk lainnya).
        asset_type: Kategori instrumen — boleh berupa AssetType atau
            string nilainya (misal "SAHAM_ID"), karena keduanya setara
            (AssetType adalah subclass str).

    Returns:
        String harga yang sudah diformat lengkap dengan simbol mata uang.
    """
    if asset_type == AssetType.IDX_STOCK:
        # Konvensi Indonesia: titik sebagai pemisah ribuan (Rp9.850.000),
        # bukan koma seperti format default Python.
        return f"Rp{value:,.0f}".replace(",", ".")
    if value >= 1:
        return f"${value:,.2f}"
    if value >= 0.01:
        return f"${value:,.4f}"
    return f"${value:,.8f}"


# ======================================================================
# 2. KONFIGURASI
# ======================================================================

def _split_env_list(env_var: str) -> List[str]:
    """Ubah "a@x.com,b@x.com" dari environment variable jadi list bersih."""
    raw = os.environ.get(env_var, "")
    return [item.strip() for item in raw.split(",") if item.strip()]


@dataclass
class Config:
    """Konfigurasi bot. Kredensial WAJIB lewat environment variable / .env,
    tidak boleh hardcode di source code."""

    # --- Daftar aset yang dipantau ---
    STOCK_ID_LIST: List[str] = field(
        default_factory=lambda: ["BBCA.JK", "TLKM.JK", "ASII.JK"]
    )
    STOCK_US_LIST: List[str] = field(default_factory=list)  # contoh: ["AAPL", "MSFT"]
    CRYPTO_LIST: List[str] = field(
        default_factory=lambda: [
            "BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD", "PEPE-USD", "BNB-USD",
        ]
    )

    # --- Kredensial & API (dari environment, boleh kosong -> fitur dilewati) ---
    NEWS_API_KEY: str = field(default_factory=lambda: os.environ.get("NEWS_API_KEY", ""))
    SENDER_EMAIL: str = field(default_factory=lambda: os.environ.get("SENDER_EMAIL", ""))
    APP_PASSWORD: str = field(
        default_factory=lambda: os.environ.get("EMAIL_APP_PASSWORD", "")
    )
    EMAIL_LIST: List[str] = field(
        default_factory=lambda: _split_env_list("RECEIVER_EMAILS")
    )

    # --- Parameter trading ---
    RISK_REWARD_RATIO: float = 2.0
    TOP_N_RECOMMENDATION: int = 3

    @property
    def ASSET_LIST(self) -> List[str]:
        return self.STOCK_ID_LIST + self.STOCK_US_LIST + self.CRYPTO_LIST


CFG = Config()


# ======================================================================
# 3. CORE: TRADING BOT (Analisis Teknikal per Aset)
# ======================================================================

class TradingBot:
    """Mengambil data pasar & menghitung sinyal teknikal untuk satu aset."""

    def __init__(self, symbol: str):
        self.symbol = symbol
        self.asset_type = classify_asset(symbol)
        self.name = symbol
        self.data = pd.DataFrame()
        self._ticker: Optional[yf.Ticker] = None

        self._fetch_history()
        self._fetch_display_name()

    def _fetch_history(self, retries: int = 3, delay_seconds: float = 2.0) -> None:
        """Ambil histori harga 1 bulan, interval 1 jam (cukup rapat untuk
        horizon swing 2-3 hari), dengan retry ringan jika hasil kosong."""
        self._ticker = yf.Ticker(self.symbol)
        for attempt in range(1, retries + 1):
            try:
                self.data = self._ticker.history(
                    period="1mo", interval="1h", auto_adjust=True
                )
                if not self.data.empty:
                    return
            except Exception as exc:  # noqa: BLE001 - data pihak ketiga, jangan crash
                logger.warning(
                    "Percobaan %d/%d gagal ambil data %s: %s",
                    attempt, retries, self.symbol, exc,
                )
            time.sleep(delay_seconds)

    def _fetch_display_name(self) -> None:
        """Ambil nama panjang emiten/aset untuk laporan. Best-effort saja —
        kalau gagal, tetap pakai ticker mentah sebagai nama."""
        try:
            info = self._ticker.info if self._ticker else {}
            display = info.get("longName") or info.get("shortName") or self.symbol
            self.name = display.split(" ")[0]
        except Exception as exc:  # noqa: BLE001
            logger.debug("Tidak bisa ambil nama untuk %s: %s", self.symbol, exc)
            self.name = self.symbol

    def _add_indicators(self) -> pd.DataFrame:
        """Hitung seluruh indikator teknikal yang dipakai untuk scoring."""
        df = self.data.copy()
        if len(df) < 30:
            return df

        # Trend jangka pendek
        df["EMA9"] = ta.trend.EMAIndicator(df["Close"], window=9).ema_indicator()
        df["EMA21"] = ta.trend.EMAIndicator(df["Close"], window=21).ema_indicator()

        # Momentum
        df["RSI"] = ta.momentum.RSIIndicator(df["Close"], window=14).rsi()
        macd = ta.trend.MACD(df["Close"])
        df["MACD"] = macd.macd()
        df["MACD_SIGNAL"] = macd.macd_signal()

        # Volume (deteksi akumulasi/distribusi)
        df["OBV"] = ta.volume.OnBalanceVolumeIndicator(
            df["Close"], df["Volume"]
        ).on_balance_volume()

        # Volatilitas & support/resistance
        bb = ta.volatility.BollingerBands(df["Close"], window=20, window_dev=2)
        df["BB_LOW"] = bb.bollinger_lband()
        df["BB_HIGH"] = bb.bollinger_hband()

        # ATR dipakai untuk menghitung SL/TP dinamis sesuai volatilitas pasar
        df["ATR"] = ta.volatility.AverageTrueRange(
            df["High"], df["Low"], df["Close"], window=14
        ).average_true_range()

        df.dropna(inplace=True)
        return df

    @staticmethod
    def _calculate_score(df: pd.DataFrame) -> Tuple[int, int, List[str]]:
        """Sistem skor sederhana: tiap kondisi teknikal yang terpenuhi
        menambah 1 poin ke buy_score atau sell_score.

        Returns:
            (buy_score, sell_score, daftar_alasan)
        """
        last = df.iloc[-1]
        buy_score, sell_score = 0, 0
        reasons: List[str] = []

        if last["EMA9"] > last["EMA21"]:
            buy_score += 1
            reasons.append("Uptrend Jangka Pendek")
        if last["MACD"] > last["MACD_SIGNAL"] and last["RSI"] < 60:
            buy_score += 1
            reasons.append("MACD Bullish")
        if last["RSI"] < 40:
            buy_score += 1
            reasons.append("RSI Area Oversold")
        if df["OBV"].iloc[-1] > df["OBV"].iloc[-5]:
            buy_score += 1
            reasons.append("Ada Akumulasi")
        if last["Close"] < last["BB_LOW"] * 1.01:
            buy_score += 1
            reasons.append("Dekat Support BB")

        if last["EMA9"] < last["EMA21"]:
            sell_score += 1
            reasons.append("Downtrend")
        if last["RSI"] > 70:
            sell_score += 1
            reasons.append("RSI Overbought")

        return buy_score, sell_score, reasons

    def analyze(self) -> Optional[Dict]:
        """Jalankan analisis penuh untuk aset ini.

        Returns:
            Dict berisi sinyal, harga, titik Buy/SL/TP; atau None kalau data
            tidak cukup untuk dianalisis.
        """
        if self.data.empty or len(self.data) < 30:
            return None

        df = self._add_indicators()
        if df.empty:
            return None

        buy_score, sell_score, reasons = self._calculate_score(df)
        price = float(df["Close"].iloc[-1])
        atr = float(df["ATR"].iloc[-1])

        if buy_score >= 3:
            signal = "STRONG BUY"
        elif buy_score >= 2:
            signal = "BUY"
        elif sell_score >= 2:
            signal = "SELL"
        else:
            signal = "HOLD"

        # SL & TP dinamis berbasis volatilitas pasar (ATR), rasio risk:reward
        # dari konfigurasi (default 1:2).
        if signal == "SELL":
            sl = price + (atr * 1.5)
            tp = price - ((sl - price) * CFG.RISK_REWARD_RATIO)
        else:
            sl = price - (atr * 1.5)
            tp = price + ((price - sl) * CFG.RISK_REWARD_RATIO)

        return {
            "Aset": self.name,
            "Kode": self.symbol,
            # Simpan sebagai string murni (bukan objek Enum langsung). Pandas
            # (versi dtype "str" terbaru) tidak selalu konsisten saat
            # membandingkan kolom secara vectorized (df["Tipe"] == enum_member)
            # dan bisa diam-diam mengembalikan False untuk semua baris. String
            # polos + `.value` saat difilter (lihat ReportGenerator) menghindari itu.
            "Tipe": self.asset_type.value,
            "Harga": price,
            "Skor": buy_score - sell_score,
            "Sinyal": signal,
            "Alasan": ", ".join(reasons[:2]) if reasons else "Tidak ada sinyal dominan",
            "Buy": price,
            "TP": tp,
            "SL": sl,
        }


# ======================================================================
# 4. AI AGENT (Opsional, best-effort — tidak boleh membuat bot gagal total)
# ======================================================================

class AIAgent:
    """Lapisan opsional: ringkasan naratif pasar via "AI gratis" DuckDuckGo.

    PENTING: fitur chat DuckDuckGo ini TIDAK RESMI (reverse-engineered, tanpa
    API key resmi), sehingga bisa berhenti berfungsi atau kena rate-limit
    kapan saja tanpa pemberitahuan — inilah sumber masalah "belum koneksi"
    di kode sebelumnya.

    Solusinya: setiap pemanggilan dibungkus retry + timeout, dan kalau tetap
    gagal (atau library-nya tidak terpasang sama sekali), sistem otomatis
    memakai `_fallback_summary()` yang berbasis aturan/teknikal murni —
    bukan AI. Dengan begitu Top-3 rekomendasi BUY beserta titik Buy/TP/SL
    tetap selalu terkirim, dengan atau tanpa AI-nya nyambung.
    """

    def __init__(self, model: str = "gpt-4o-mini", timeout: int = 20, max_retries: int = 2):
        self.model = model
        self.timeout = timeout
        self.max_retries = max_retries
        self.available = _DDGS_AVAILABLE

        if not self.available:
            logger.info(
                "Library ddgs/duckduckgo_search tidak terpasang — "
                "AI ringkasan akan pakai mode fallback (berbasis teknikal)."
            )

    def _call_ddgs_chat(self, prompt: str) -> Optional[str]:
        """Coba hubungi AI gratis DuckDuckGo dengan retry. None jika gagal."""
        if not self.available:
            return None

        for attempt in range(1, self.max_retries + 1):
            try:
                with DDGS(timeout=self.timeout) as ddgs:
                    return ddgs.chat(prompt, model=self.model)
            except Exception as exc:  # noqa: BLE001 - layanan pihak ketiga tak resmi
                logger.warning(
                    "Percobaan %d/%d gagal hubungi AI DuckDuckGo: %s",
                    attempt, self.max_retries, exc,
                )
                time.sleep(2 * attempt)
        return None

    def buat_analisis_lengkap(
        self, df: pd.DataFrame, berita: List[Dict], sentimen: str
    ) -> Dict:
        """Hasilkan ringkasan pasar + rekomendasi Top-N BUY.

        Selalu mengembalikan struktur yang valid (via AI atau fallback),
        jadi pemanggil tidak perlu menangani kegagalan AI secara khusus.
        """
        top_buy = (
            df[df["Sinyal"].isin(["BUY", "STRONG BUY"])]
            .sort_values("Skor", ascending=False)
            .head(CFG.TOP_N_RECOMMENDATION)
        )
        news_titles = " ".join(n.get("title", "") for n in berita[:3])

        raw_response = self._call_ddgs_chat(
            self._build_prompt(top_buy, sentimen, news_titles)
        )

        if raw_response:
            parsed = self._parse_ai_json(raw_response)
            if parsed:
                return parsed
            logger.warning("Respons AI bukan JSON valid, memakai ringkasan fallback.")

        return self._fallback_summary(top_buy, sentimen)

    @staticmethod
    def _build_prompt(top_buy: pd.DataFrame, sentimen: str, news_titles: str) -> str:
        return (
            "Kamu adalah analis keuangan senior. Balas HANYA dengan JSON valid, "
            "tanpa markdown atau code block.\n"
            f"Data kandidat BUY (skor teknikal tertinggi): {top_buy.to_json(orient='records')}\n"
            f"Sentimen pasar saat ini: {sentimen}. Judul berita terkait: {news_titles}\n"
            "Format JSON WAJIB:\n"
            '{"ringkasan_pasar": "3 kalimat analisis pasar", '
            '"rekomendasi": [{"aset": "", "kode": "", "aksi": "BUY", '
            '"alasan": "", "buy": 0, "tp": 0, "sl": 0}], '
            '"peringatan_risiko": "1 kalimat peringatan"}'
        )

    @staticmethod
    def _parse_ai_json(raw: str) -> Optional[Dict]:
        """Bersihkan & parse output AI. None kalau formatnya tidak sesuai."""
        cleaned = raw.replace("```json", "").replace("```", "").strip()
        try:
            data = json.loads(cleaned)
        except (json.JSONDecodeError, TypeError):
            return None
        return data if isinstance(data, dict) and "rekomendasi" in data else None

    @staticmethod
    def _fallback_summary(top_buy: pd.DataFrame, sentimen: str) -> Dict:
        """Ringkasan berbasis aturan (tanpa AI) — dipakai saat AI gratis
        DuckDuckGo tidak terhubung. Tetap menghasilkan Top-N rekomendasi
        dengan titik Buy/TP/SL, hanya narasinya yang templated."""
        rekomendasi = [
            {
                "aset": row["Aset"],
                "kode": row["Kode"],
                "aksi": row["Sinyal"],
                "alasan": row["Alasan"],
                "buy": row["Buy"],
                "tp": row["TP"],
                "sl": row["SL"],
            }
            for _, row in top_buy.iterrows()
        ]

        if rekomendasi:
            ringkasan = (
                f"Sentimen pasar saat ini {sentimen.lower()}. "
                f"Ditemukan {len(rekomendasi)} aset dengan sinyal beli terkuat "
                "berdasarkan kombinasi EMA, RSI, MACD, dan volume (OBV)."
            )
        else:
            ringkasan = (
                f"Sentimen pasar saat ini {sentimen.lower()}, namun belum ada "
                "sinyal beli yang cukup kuat pada pemindaian kali ini."
            )

        return {
            "ringkasan_pasar": ringkasan,
            "rekomendasi": rekomendasi,
            "peringatan_risiko": (
                "Ringkasan ini dihasilkan otomatis dari indikator teknikal "
                "(mode cadangan, AI sedang tidak terhubung). Selalu lakukan "
                "riset mandiri (DYOR) sebelum bertransaksi."
            ),
        }


# ======================================================================
# 5. BERITA & SENTIMEN
# ======================================================================

class NewsService:
    """Ambil berita pasar terkini untuk konteks sentimen. Bersifat opsional —
    kalau NEWS_API_KEY kosong atau request gagal, sentimen default NETRAL."""

    @staticmethod
    def get_market_news(api_key: str) -> Tuple[List[Dict], str]:
        if not api_key:
            logger.info("NEWS_API_KEY tidak diset — melewati pengambilan berita.")
            return [], "NETRAL"

        try:
            response = requests.get(
                "https://newsapi.org/v2/everything",
                params={
                    "q": "IDX OR Bitcoin",
                    "language": "en",
                    "sortBy": "publishedAt",
                    "pageSize": 3,
                    "apiKey": api_key,
                },
                timeout=10,
            )
            response.raise_for_status()
            articles = response.json().get("articles", [])
        except requests.RequestException as exc:
            logger.warning("Gagal mengambil berita: %s", exc)
            return [], "NETRAL"

        text = " ".join(a.get("title", "") for a in articles).lower()
        if any(k in text for k in ("surge", "bull", "rally")):
            sentiment = "BULLISH"
        elif any(k in text for k in ("crash", "bear", "plunge")):
            sentiment = "BEARISH"
        else:
            sentiment = "NETRAL"
        return articles, sentiment


# ======================================================================
# 6. LAPORAN HTML
# ======================================================================

class ReportGenerator:
    """Menyusun laporan HTML: ringkasan AI, Top-N rekomendasi, dan tabel
    sinyal teknikal terpisah per jenis instrumen (Saham ID / Saham US / Kripto)."""

    _SECTION_TITLES: Dict[AssetType, str] = {
        AssetType.IDX_STOCK: "📈 Saham Indonesia (IDX)",
        AssetType.US_STOCK: "🌎 Saham Global (US)",
        AssetType.CRYPTO: "🪙 Kripto",
    }

    _STYLE = """
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        * { box-sizing: border-box; }
        body { font-family: 'Segoe UI', Arial, sans-serif; background: #f4f6f8; margin: 0; padding: 10px; color: #333; line-height: 1.5; }
        .container { max-width: 800px; margin: auto; background: #fff; border-radius: 12px; overflow: hidden; box-shadow: 0 4px 20px rgba(0,0,0,0.05); }
        .header { background: linear-gradient(90deg, #1a2b4c, #2563eb); color: white; padding: 25px 15px; text-align: center; }
        .header h1 { margin: 0; font-size: 22px; }
        .header p { margin: 5px 0 0 0; font-size: 14px; opacity: 0.9; }
        .section { padding: 20px 15px; border-bottom: 1px solid #eee; }
        .ai-box { background: #eef5ff; border-left: 4px solid #2563eb; padding: 15px; border-radius: 4px; margin-bottom: 20px; }
        h2, h3 { margin-top: 0; color: #1a2b4c; }
        .table-wrapper { overflow-x: auto; width: 100%; -webkit-overflow-scrolling: touch; margin-bottom: 18px; }
        table { width: 100%; border-collapse: collapse; font-size: 13px; min-width: 500px; }
        th { background: #f8fafc; color: #1e293b; padding: 12px; text-align: left; font-weight: 600; }
        td { padding: 12px; border-bottom: 1px solid #f1f5f9; }
        .rec-table th { background: #1a2b4c; color: white; }
        .badge { padding: 4px 8px; border-radius: 6px; font-size: 11px; font-weight: bold; color: white; display: inline-block;}
        .strongbuy { background: #16a34a; } .buy { background: #22c55e; }
        .sell { background: #ef4444; } .hold { background: #f59e0b; color: #fff; }
        .footer { padding: 20px; text-align: center; font-size: 12px; color: #64748b; }
        @media only screen and (max-width: 600px) { .section { padding: 15px 10px; } table { font-size: 12px; } }
    </style>
    """

    @classmethod
    def _recommendation_rows(cls, rekomendasi: List[Dict], df: pd.DataFrame) -> str:
        """Baris tabel Top-N rekomendasi, dengan mata uang otomatis sesuai
        jenis instrumen tiap aset (dilihat dari kode/ticker-nya)."""
        type_by_code = df.set_index("Kode")["Tipe"].to_dict()
        rows = []
        for r in rekomendasi:
            asset_type = type_by_code.get(r.get("kode"), AssetType.US_STOCK.value)
            badge_class = str(r.get("aksi", "")).lower().replace(" ", "")
            rows.append(
                f"<tr><td><b>{r.get('aset')}</b></td>"
                f"<td><span class='badge {badge_class}'>{r.get('aksi')}</span></td>"
                f"<td>{r.get('alasan')}</td>"
                f"<td>{format_currency(r.get('buy', 0), asset_type)}</td>"
                f"<td>{format_currency(r.get('tp', 0), asset_type)}</td>"
                f"<td>{format_currency(r.get('sl', 0), asset_type)}</td></tr>"
            )
        return "".join(rows)

    @classmethod
    def _asset_table_rows(cls, subset: pd.DataFrame) -> str:
        """Baris tabel pemindai teknikal untuk satu jenis instrumen."""
        rows = []
        for _, r in subset.iterrows():
            badge_class = r["Sinyal"].lower().replace(" ", "")
            rows.append(
                f"<tr><td><b>{r['Aset']}</b><br>"
                f"<span style='color:#64748b;font-size:11px;'>{r['Kode']}</span></td>"
                f"<td>{format_currency(r['Harga'], r['Tipe'])}</td>"
                f"<td><span class='badge {badge_class}'>{r['Sinyal']}</span></td>"
                f"<td>{r['Alasan']}</td></tr>"
            )
        return "".join(rows)

    @classmethod
    def _build_asset_sections(cls, df: pd.DataFrame) -> str:
        """Satu tabel terpisah per jenis instrumen. Instrumen tanpa data
        (contoh: STOCK_US_LIST kosong) otomatis dilewati."""
        sections = []
        for asset_type, title in cls._SECTION_TITLES.items():
            # Bandingkan dengan .value (string murni) — lihat catatan di
            # TradingBot.analyze() soal kolom "Tipe".
            subset = df[df["Tipe"] == asset_type.value]
            if subset.empty:
                continue
            sections.append(
                f"<h3>{title}</h3>"
                "<div class='table-wrapper'><table>"
                "<tr><th>Aset</th><th>Harga</th><th>Sinyal</th><th>Indikator Pemicu</th></tr>"
                f"{cls._asset_table_rows(subset)}"
                "</table></div>"
            )
        return "".join(sections)

    @classmethod
    def create_html_report(
        cls, df: pd.DataFrame, ai_result: Dict, sentiment: str, time_of_day: str
    ) -> str:
        date_str = datetime.now().strftime("%d %B %Y, %H:%M WIB")

        rec_rows = cls._recommendation_rows(ai_result.get("rekomendasi", []), df)
        empty_rec_row = (
            "<tr><td colspan='6' style='text-align:center;'>"
            "Tidak ada rekomendasi dominan saat ini.</td></tr>"
        )

        return f"""
        <html>
        <head>{cls._STYLE}</head>
        <body>
            <div class='container'>
                <div class='header'>
                    <h1>🤖 AI Trading Agent Pro</h1>
                    <p>{time_of_day} | {date_str}</p>
                    <p>Sentimen Global: <b>{sentiment}</b></p>
                </div>
                <div class='section'>
                    <div class='ai-box'>
                        <h2>✨ Ringkasan Pasar</h2>
                        <p>{ai_result.get('ringkasan_pasar', '')}</p>
                    </div>
                    <h3>🏆 Top {CFG.TOP_N_RECOMMENDATION} Rekomendasi Beli</h3>
                    <div class='table-wrapper'>
                        <table class='rec-table'>
                            <tr><th>Aset</th><th>Aksi</th><th>Alasan</th><th>Buy</th><th>TP</th><th>SL</th></tr>
                            {rec_rows or empty_rec_row}
                        </table>
                    </div>
                    <p style='margin-top:15px;color:#dc3545;font-size:13px;'>
                        <b>Peringatan:</b> {ai_result.get('peringatan_risiko', '')}
                    </p>
                </div>
                <div class='section'>
                    <h2>📊 Pemindai Sinyal Teknikal (Per Jam)</h2>
                    {cls._build_asset_sections(df)}
                </div>
                <div class='footer'>
                    Disclaimer: Analisis otomatis, bukan nasihat keuangan.
                    Selalu gunakan manajemen risiko mandiri.
                </div>
            </div>
        </body>
        </html>
        """


# ======================================================================
# 7. EMAIL
# ======================================================================

class EmailService:
    """Kirim laporan HTML ke daftar penerima via Gmail SMTP."""

    @staticmethod
    def send_report(subject: str, html_body: str) -> None:
        if not (CFG.SENDER_EMAIL and CFG.APP_PASSWORD and CFG.EMAIL_LIST):
            logger.error(
                "Konfigurasi email belum lengkap (SENDER_EMAIL / "
                "EMAIL_APP_PASSWORD / RECEIVER_EMAILS di .env). Email tidak dikirim."
            )
            return

        for receiver in CFG.EMAIL_LIST:
            message = MIMEMultipart()
            message["From"] = CFG.SENDER_EMAIL
            message["To"] = receiver
            message["Subject"] = subject
            message.attach(MIMEText(html_body, "html"))

            try:
                with smtplib.SMTP("smtp.gmail.com", 587) as server:
                    server.starttls()
                    server.login(CFG.SENDER_EMAIL, CFG.APP_PASSWORD)
                    server.send_message(message)
                logger.info("Email terkirim ke: %s", receiver)
            except smtplib.SMTPException as exc:
                logger.error("Gagal mengirim ke %s: %s", receiver, exc)


# ======================================================================
# 8. MAIN
# ======================================================================

def run_trading_agent() -> None:
    """Orkestrasi penuh: ambil data -> analisis -> AI (opsional) -> email."""
    logger.info("AI Trading Agent dimulai (%d aset dipantau).", len(CFG.ASSET_LIST))

    results = []
    for symbol in CFG.ASSET_LIST:
        bot = TradingBot(symbol)
        result = bot.analyze()
        if result:
            results.append(result)
        time.sleep(0.5)  # jaga-jaga agar tidak kena rate-limit Yahoo Finance

    if not results:
        logger.error("Tidak ada data pasar yang berhasil diambil. Proses dihentikan.")
        return

    df_results = pd.DataFrame(results).sort_values("Skor", ascending=False)

    logger.info("Mengambil berita & menyusun ringkasan pasar...")
    news, sentiment = NewsService.get_market_news(CFG.NEWS_API_KEY)
    ai_result = AIAgent().buat_analisis_lengkap(df_results, news, sentiment)

    time_label = "Laporan Sesi Pagi" if datetime.now().hour < 12 else "Laporan Sesi Sore"
    subject = f"[AI SWING BOT] {time_label} | {sentiment} | {datetime.now():%d %b}"

    logger.info("Menyusun & mengirim laporan email...")
    html_body = ReportGenerator.create_html_report(df_results, ai_result, sentiment, time_label)
    EmailService.send_report(subject, html_body)

    logger.info("Selesai.")


if __name__ == "__main__":
    run_trading_agent()
