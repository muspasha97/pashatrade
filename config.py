"""
config.py
---------
Sistemin tum yapilandirma sabitlerini tek bir yerde toplar. Coin gruplari,
Excel dosyalari, API endpoint'leri, zaman dilimleri, risk parametreleri.
Hicbir is mantigi icermez - sadece konstantlar.
"""
from __future__ import annotations
from pathlib import Path

# ---------- DIZIN YAPILANDIRMA ----------
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
STATE_DIR = BASE_DIR / "state"
RULES_DIR = BASE_DIR / "rules"
LOGS_DIR = BASE_DIR / "logs"

# Klasorler yoksa olustur (idempotent)
for _d in (DATA_DIR, STATE_DIR, RULES_DIR, LOGS_DIR):
    _d.mkdir(parents=True, exist_ok=True)


# ---------- COIN GRUPLARI ----------
# Her grup ayri bir Excel dosyasinda saklanir.
# Sembol format: "BTC/USDT" (ccxt unified). Futures de ayni sembol kullanir.
COIN_GROUPS: dict[str, list[str]] = {
    "majors":    ["BTC/USDT", "ETH/USDT"],
    "layer1":    ["SOL/USDT", "BNB/USDT", "XRP/USDT", "ADA/USDT", "DOT/USDT"],
    "midcap":    ["AVAX/USDT", "LINK/USDT", "ATOM/USDT", "NEAR/USDT"],
    "highbeta":  ["DOGE/USDT", "SHIB/USDT", "PEPE/USDT"],
    "defi":      ["UNI/USDT", "AAVE/USDT"],
    "layer2":    ["ARB/USDT", "OP/USDT", "MATIC/USDT"],
    "ai":        ["FET/USDT"],
}

# Tum coinlerin duz listesi (lookup icin)
ALL_COINS: list[str] = [c for group in COIN_GROUPS.values() for c in group]

# Coin -> grup adi (Excel dosyasi yonlendirmesi icin)
COIN_TO_GROUP: dict[str, str] = {
    coin: grp for grp, coins in COIN_GROUPS.items() for coin in coins
}

# Excel dosya isimleri
def excel_path_for_group(group: str) -> Path:
    return DATA_DIR / f"trade_{group}.xlsx"


# ---------- ZAMAN DILIMLERI ----------
# Multi-timeframe analiz icin kullanilan kline TF'leri
TIMEFRAMES = ["5m", "15m", "1h", "4h", "1d"]
PRIMARY_TF = "15m"  # birincil dongu TF


# ---------- API ENDPOINTS ----------
# Binance Futures "futures/data" endpoint'leri (ccxt kapsaminda DEGIL)
BINANCE_FUTURES_BASE = "https://fapi.binance.com"
FAPI_OI_HIST           = f"{BINANCE_FUTURES_BASE}/futures/data/openInterestHist"
FAPI_TOP_LSPOS_RATIO   = f"{BINANCE_FUTURES_BASE}/futures/data/topLongShortPositionRatio"
FAPI_TOP_LSACC_RATIO   = f"{BINANCE_FUTURES_BASE}/futures/data/topLongShortAccountRatio"
FAPI_GLOBAL_LS_RATIO   = f"{BINANCE_FUTURES_BASE}/futures/data/globalLongShortAccountRatio"
FAPI_TAKER_RATIO       = f"{BINANCE_FUTURES_BASE}/futures/data/takerlongshortRatio"

# Sentiment & makro
ALTERNATIVE_ME_FNG = "https://api.alternative.me/fng/?limit={limit}"
COINGECKO_GLOBAL   = "https://api.coingecko.com/api/v3/global"


# ---------- RATE LIMIT VE TIMEOUT ----------
HTTP_TIMEOUT_SEC = 10
HTTP_MAX_RETRIES = 3
HTTP_RETRY_BACKOFF_SEC = 2.0  # exponential: 2, 4, 8


# ---------- RISK VE TRADE PARAMETRELERI ----------
# ATR tabanli dinamik hedef/stop
ATR_TARGET_MULT = 2.0   # TP = entry +/- 2 * ATR
ATR_STOP_MULT   = 1.0   # SL = entry -/+ 1 * ATR  (R:R = 1:2)

# Pozisyon sizing
RISK_PER_TRADE_USD = 100.0  # her sanal islem icin risk
DEFAULT_NOTIONAL_USD = 1000.0  # paper trade notional

# Karar motoru esikleri
DECISION_THRESHOLD_LONG  = 0.55   # skor > 0.55 -> long sinyali
DECISION_THRESHOLD_SHORT = -0.55  # skor < -0.55 -> short sinyali
MIN_CONFIDENCE = 0.50             # bunun altinda islem acma


# ---------- SPOOFING TESPIT ESIK ----------
# Bir bid/ask emrin "duvar" sayilabilmesi icin minimum buyukluk (top 10 ort * X)
WALL_DETECT_MULTIPLIER = 5.0
# Spoofing: duvar varsa, simdi yoksa, bu fiyatta trade gormediyse
SPOOFING_PRICE_TOLERANCE_PCT = 0.05  # %0.05 fiyat bandi


# ---------- LIKIDASYON PROXY ----------
# WebSocket olmadigindan likidasyon dogrudan cekilemiyor.
# Proxy: OI ani dusus + ayni yonde fiyat hareketi -> likidasyon eventi tahmin.
LIQ_PROXY_OI_DROP_PCT = 2.0     # OI 5dk'da > %2 dustuyse
LIQ_PROXY_PRICE_MOVE_PCT = 0.5  # ve fiyat ayni yonde > %0.5 hareket ettiyse
ENABLE_LIQUIDATION = True       # proxy aktif


# ---------- VERI CERCEVE BUYUKLUKLERI ----------
OHLCV_LIMIT_1M = 60          # 1 saat 1dk
OHLCV_LIMIT_5M = 60          # 5 saat 5dk
OHLCV_LIMIT_15M = 100        # ~25 saat
OHLCV_LIMIT_1H = 100         # ~4 gun
OHLCV_LIMIT_4H = 50          # ~8 gun
OHLCV_LIMIT_1D = 30          # 30 gun
ORDER_BOOK_LIMIT = 500       # uc top'a kadar derinlik
TRADES_LIMIT = 1000          # son 1000 trade


# ---------- LOGGING ----------
LOG_LEVEL = "INFO"
LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)-15s | %(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"
