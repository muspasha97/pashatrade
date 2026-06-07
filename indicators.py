"""
indicators.py
-------------
Teknik indikator ve fiyat-aksiyonu hesaplamalari. Tum hesaplamalar
SymbolBundle'daki OHLCV verisinden yapilir, dis API cagrisi YOK.

Pandas + pandas_ta kombinasyonu. pandas_ta yoksa numpy fallback.
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from src.utils import safe_pct_change, safe_ratio, setup_logger

logger = setup_logger("indicators")

try:
    import pandas_ta as ta
    HAVE_PANDAS_TA = True
except ImportError:
    HAVE_PANDAS_TA = False
    logger.warning("pandas_ta yok - manuel RSI/ATR hesaplari kullanilacak")


class IndicatorCalculator:
    """OHLCV verisinden teknik indikator hesaplamalari."""

    def __init__(self) -> None:
        self.logger = logger

    # ============= TEMEL DONUSTURUCULER =============
    def ohlcv_to_df(self, ohlcv: list[list[float]]) -> pd.DataFrame:
        """ccxt OHLCV formatini DataFrame'e cevirir."""
        if not ohlcv:
            return pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume"])
        df = pd.DataFrame(
            ohlcv,
            columns=["ts", "open", "high", "low", "close", "volume"],
        )
        df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = df[col].astype(float)
        return df

    # ============= TREND ANALIZI =============
    def compute_trend(self, ohlcv: list[list[float]]) -> str:
        """
        Son 3 mum yon tayini. Kullanicinin istedigi btc_trendi formati.
        Donusler: yukselis | dusus | yatay
        """
        if not ohlcv or len(ohlcv) < 3:
            return "yatay"
        closes = [c[4] for c in ohlcv[-3:]]
        # 3 ardisik yesil
        if closes[2] > closes[1] > closes[0]:
            return "yukselis"
        # 3 ardisik kirmizi
        if closes[2] < closes[1] < closes[0]:
            return "dusus"
        # Net hareket %0.5'ten az ise yatay
        change = safe_pct_change(closes[2], closes[0])
        if abs(change) < 0.5:
            return "yatay"
        return "yukselis" if change > 0 else "dusus"

    def compute_pct_change(
        self, df: pd.DataFrame, lookback: int = 1
    ) -> float:
        """Son N mum kapanis arasi yuzde degisim."""
        if len(df) < lookback + 1:
            return 0.0
        curr = df["close"].iloc[-1]
        prev = df["close"].iloc[-lookback - 1]
        return safe_pct_change(curr, prev)

    # ============= RSI =============
    def compute_rsi(self, df: pd.DataFrame, length: int = 14) -> float:
        """RSI 14. pandas_ta varsa onu, yoksa manuel hesap."""
        if len(df) < length + 1:
            return 50.0
        try:
            if HAVE_PANDAS_TA:
                rsi = ta.rsi(df["close"], length=length)
                if rsi is None or rsi.empty:
                    return 50.0
                return float(rsi.iloc[-1]) if not pd.isna(rsi.iloc[-1]) else 50.0
            else:
                return self._manual_rsi(df["close"], length)
        except Exception as e:
            self.logger.warning(f"RSI hata: {e}")
            return 50.0

    @staticmethod
    def _manual_rsi(close: pd.Series, length: int = 14) -> float:
        """Wilder smoothing RSI - pandas_ta yoksa fallback."""
        diff = close.diff()
        gain = diff.where(diff > 0, 0.0)
        loss = -diff.where(diff < 0, 0.0)
        avg_gain = gain.ewm(alpha=1 / length, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1 / length, adjust=False).mean()
        rs = avg_gain / avg_loss.replace(0, np.nan)
        rsi = 100 - (100 / (1 + rs))
        return float(rsi.iloc[-1]) if not pd.isna(rsi.iloc[-1]) else 50.0

    # ============= MACD =============
    def compute_macd(self, df: pd.DataFrame) -> dict[str, float]:
        """MACD (12, 26, 9). Line, signal, histogram."""
        if len(df) < 35:
            return {"line": 0.0, "signal": 0.0, "hist": 0.0}
        try:
            ema12 = df["close"].ewm(span=12, adjust=False).mean()
            ema26 = df["close"].ewm(span=26, adjust=False).mean()
            line = ema12 - ema26
            signal = line.ewm(span=9, adjust=False).mean()
            hist = line - signal
            return {
                "line": float(line.iloc[-1]),
                "signal": float(signal.iloc[-1]),
                "hist": float(hist.iloc[-1]),
            }
        except Exception as e:
            self.logger.warning(f"MACD hata: {e}")
            return {"line": 0.0, "signal": 0.0, "hist": 0.0}

    # ============= ATR (Average True Range) =============
    def compute_atr(self, df: pd.DataFrame, length: int = 14) -> float:
        """ATR - dinamik TP/SL hesabinda kullanilir."""
        if len(df) < length + 1:
            return 0.0
        try:
            high = df["high"]
            low = df["low"]
            close = df["close"]
            tr1 = high - low
            tr2 = (high - close.shift()).abs()
            tr3 = (low - close.shift()).abs()
            tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
            atr = tr.ewm(alpha=1 / length, adjust=False).mean()
            val = float(atr.iloc[-1])
            return val if not pd.isna(val) else 0.0
        except Exception as e:
            self.logger.warning(f"ATR hata: {e}")
            return 0.0

    def compute_atr_pct(self, df: pd.DataFrame, length: int = 14) -> float:
        """ATR'nin fiyata orani - normalize volatilite."""
        atr = self.compute_atr(df, length)
        if df.empty or atr == 0:
            return 0.0
        return safe_ratio(atr, df["close"].iloc[-1]) * 100.0

    # ============= BOLLINGER BANDS =============
    def compute_bbands(
        self, df: pd.DataFrame, length: int = 20, std: float = 2.0
    ) -> dict[str, float]:
        """BB - orta, ust, alt, %B, bandwidth."""
        if len(df) < length:
            return {"mid": 0.0, "upper": 0.0, "lower": 0.0, "pct_b": 0.5, "width": 0.0}
        try:
            mid = df["close"].rolling(length).mean()
            sd = df["close"].rolling(length).std()
            upper = mid + std * sd
            lower = mid - std * sd
            curr = df["close"].iloc[-1]
            m, u, l = float(mid.iloc[-1]), float(upper.iloc[-1]), float(lower.iloc[-1])
            pct_b = safe_ratio(curr - l, u - l, default=0.5)
            width = safe_ratio(u - l, m)
            return {"mid": m, "upper": u, "lower": l, "pct_b": pct_b, "width": width}
        except Exception as e:
            self.logger.warning(f"BB hata: {e}")
            return {"mid": 0.0, "upper": 0.0, "lower": 0.0, "pct_b": 0.5, "width": 0.0}

    # ============= VWAP =============
    def compute_vwap(self, df: pd.DataFrame) -> float:
        """
        VWAP - tipik fiyat (HLC/3) hacim agirlikli.
        DataFrame ne kadar uzunsa o kadar zaman dilimini kapsar.
        Gunluk VWAP icin 1dk veya 5dk barlar verilmeli.
        """
        if df.empty:
            return 0.0
        try:
            typical = (df["high"] + df["low"] + df["close"]) / 3.0
            cum_vol = df["volume"].cumsum()
            cum_pv = (typical * df["volume"]).cumsum()
            vwap = cum_pv / cum_vol.replace(0, np.nan)
            val = float(vwap.iloc[-1])
            return val if not pd.isna(val) else float(df["close"].iloc[-1])
        except Exception as e:
            self.logger.warning(f"VWAP hata: {e}")
            return float(df["close"].iloc[-1]) if not df.empty else 0.0

    def compute_vwap_deviation(self, current_price: float, vwap: float) -> float:
        """Anlik fiyatin VWAP'a yuzde mesafesi."""
        if vwap == 0:
            return 0.0
        return (current_price - vwap) / vwap * 100.0

    # ============= VOLUME PROFILE - POC =============
    def compute_volume_profile(
        self, df: pd.DataFrame, num_bins: int = 50
    ) -> dict[str, float]:
        """
        Volume profile: POC (Point of Control), VAH, VAL.
        DataFrame son 24 saatlik 5dk barlar olmali (~288 satir).
        Sade hesap: high-low araligi bin'lere bolunur, her bin'in hacmi hesaplanir.
        """
        if df.empty or len(df) < 10:
            return {"poc": 0.0, "vah": 0.0, "val": 0.0}
        try:
            price_min = df["low"].min()
            price_max = df["high"].max()
            if price_max <= price_min:
                return {"poc": float(price_min), "vah": float(price_max), "val": float(price_min)}

            bin_edges = np.linspace(price_min, price_max, num_bins + 1)
            bin_centers = (bin_edges[:-1] + bin_edges[1:]) / 2.0
            volumes = np.zeros(num_bins)

            # Her mum hacmini tipik fiyatin dustugu bin'e at
            typical = (df["high"] + df["low"] + df["close"]) / 3.0
            for tp, vol in zip(typical, df["volume"]):
                idx = int((tp - price_min) / (price_max - price_min) * num_bins)
                idx = min(max(idx, 0), num_bins - 1)
                volumes[idx] += vol

            # POC = en cok hacim olan bin
            poc_idx = int(np.argmax(volumes))
            poc = float(bin_centers[poc_idx])

            # Value Area: %70 hacmin oldugu bolge
            total_vol = volumes.sum()
            target = total_vol * 0.70
            cumulative = volumes[poc_idx]
            lower_idx, upper_idx = poc_idx, poc_idx
            while cumulative < target and (lower_idx > 0 or upper_idx < num_bins - 1):
                up_vol = volumes[upper_idx + 1] if upper_idx + 1 < num_bins else 0
                dn_vol = volumes[lower_idx - 1] if lower_idx - 1 >= 0 else 0
                if up_vol >= dn_vol and upper_idx + 1 < num_bins:
                    upper_idx += 1
                    cumulative += up_vol
                elif lower_idx - 1 >= 0:
                    lower_idx -= 1
                    cumulative += dn_vol
                else:
                    break

            vah = float(bin_centers[upper_idx])
            val = float(bin_centers[lower_idx])
            return {"poc": poc, "vah": vah, "val": val}
        except Exception as e:
            self.logger.warning(f"Volume profile hata: {e}")
            return {"poc": 0.0, "vah": 0.0, "val": 0.0}

    def compute_poc_distance(self, current_price: float, poc: float) -> float:
        """POC'tan yuzde mesafe."""
        if poc == 0:
            return 0.0
        return (current_price - poc) / poc * 100.0

    # ============= VOLATILITE =============
    def compute_realized_vol(self, df: pd.DataFrame, lookback: int = 24) -> float:
        """
        Realized volatility (annualized %).
        df: saatlik veya 15dk barlar.
        """
        if len(df) < lookback:
            return 0.0
        try:
            returns = df["close"].pct_change().dropna().tail(lookback)
            if len(returns) < 2:
                return 0.0
            std = returns.std()
            # Annualization factor for hourly: sqrt(24 * 365)
            annual_factor = np.sqrt(24 * 365)
            return float(std * annual_factor * 100.0)
        except Exception:
            return 0.0
