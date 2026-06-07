"""
trader.py
---------
Sanal islem motoru. Iki sorumluluk:
1. Yeni islem ac: sinyal varsa entry/TP/SL hesabi yapip satir hazirlar.
2. Mevcut bekleyen islemleri kontrol et: anlik fiyat TP/SL'ye degdi mi?

ATR tabanli dinamik hedef:
  TP = entry +/- (2 * ATR)
  SL = entry -/+ (1 * ATR)  (R:R = 1:2)
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pandas as pd

from src import config
from src.utils import now_utc_iso, setup_logger

logger = setup_logger("trader")


class PaperTrader:
    """Sanal islem acma ve takip motoru."""

    def __init__(self) -> None:
        self.logger = logger

    # ============= YENI ISLEM AC =============
    def open_trade(
        self,
        symbol: str,
        decision: dict[str, Any],
        current_price: float,
        atr: float,
        features: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Yeni sanal islem satiri hazirlar.
        decision: {direction, score, confidence, red_set_tag}
        atr: ATR degeri (mutlak)
        features: tum feature dict (Excel satirina yazilacak)

        Donus: islem satiri (dict). islem_durumu = 'bekliyor'.
        """
        direction = decision["direction"]
        if direction == "neutral":
            return {}

        # ATR yoksa veya cok kucukse fiyatin %1 stop, %2 target
        if atr <= 0 or atr < current_price * 0.001:
            atr = current_price * 0.005  # %0.5 fallback

        if direction == "long":
            target = current_price + (config.ATR_TARGET_MULT * atr)
            stop = current_price - (config.ATR_STOP_MULT * atr)
        else:  # short
            target = current_price - (config.ATR_TARGET_MULT * atr)
            stop = current_price + (config.ATR_STOP_MULT * atr)

        # Pozisyon boyutu hesabi (basit Kelly): risk_per_trade / stop_distance
        stop_distance = abs(current_price - stop)
        if stop_distance > 0:
            position_size_usd = (config.RISK_PER_TRADE_USD / stop_distance) * current_price
            position_size_usd = min(position_size_usd, config.DEFAULT_NOTIONAL_USD)
        else:
            position_size_usd = config.DEFAULT_NOTIONAL_USD

        row = {
            "tarih": now_utc_iso(),
            "tahmin_yonu": direction,
            "giris_fiyati": round(current_price, 6),
            "hedef_fiyat": round(target, 6),
            "stop_fiyati": round(stop, 6),
            "islem_durumu": "bekliyor",
            "kirmizi_set_etiketi": decision.get("red_set_tag", ""),
            # Ek meta
            "skor": decision.get("score", 0.0),
            "confidence": decision.get("confidence", 0.0),
            "atr_kullanildi": round(atr, 6),
            "pozisyon_buyukluk_usd": round(position_size_usd, 2),
        }
        # Tum feature'lari da row'a kaynastir
        for k, v in features.items():
            if k not in row:  # cakisma onleme
                row[k] = v

        self.logger.info(
            f"YENI ISLEM {symbol} {direction.upper()} @ {current_price:.4f} "
            f"TP={target:.4f} SL={stop:.4f} ATR={atr:.4f}"
        )
        return row

    # ============= BEKLEYEN ISLEM KONTROL =============
    def check_pending_trades(
        self,
        df: pd.DataFrame,
        current_price: float,
    ) -> tuple[pd.DataFrame, int, int]:
        """
        DataFrame'de islem_durumu == 'bekliyor' olan satirlari tara.
        Fiyat TP'ye degdiyse -> 'basarili'
        Fiyat SL'ye degdiyse -> 'basarisiz'

        Donus: (guncellenmis_df, basarili_sayi, basarisiz_sayi)
        """
        if df.empty:
            return df, 0, 0

        if "islem_durumu" not in df.columns:
            return df, 0, 0

        success = 0
        fail = 0
        now = now_utc_iso()

        for idx in df.index:
            try:
                status = df.at[idx, "islem_durumu"]
                if status != "bekliyor":
                    continue

                direction = df.at[idx, "tahmin_yonu"]
                target = float(df.at[idx, "hedef_fiyat"])
                stop = float(df.at[idx, "stop_fiyati"])

                if direction == "long":
                    if current_price >= target:
                        df.at[idx, "islem_durumu"] = "basarili"
                        if "cikis_fiyati" in df.columns:
                            df.at[idx, "cikis_fiyati"] = round(current_price, 6)
                        if "kapanis_tarihi" in df.columns:
                            df.at[idx, "kapanis_tarihi"] = now
                        success += 1
                    elif current_price <= stop:
                        df.at[idx, "islem_durumu"] = "basarisiz"
                        if "cikis_fiyati" in df.columns:
                            df.at[idx, "cikis_fiyati"] = round(current_price, 6)
                        if "kapanis_tarihi" in df.columns:
                            df.at[idx, "kapanis_tarihi"] = now
                        fail += 1
                elif direction == "short":
                    if current_price <= target:
                        df.at[idx, "islem_durumu"] = "basarili"
                        if "cikis_fiyati" in df.columns:
                            df.at[idx, "cikis_fiyati"] = round(current_price, 6)
                        if "kapanis_tarihi" in df.columns:
                            df.at[idx, "kapanis_tarihi"] = now
                        success += 1
                    elif current_price >= stop:
                        df.at[idx, "islem_durumu"] = "basarisiz"
                        if "cikis_fiyati" in df.columns:
                            df.at[idx, "cikis_fiyati"] = round(current_price, 6)
                        if "kapanis_tarihi" in df.columns:
                            df.at[idx, "kapanis_tarihi"] = now
                        fail += 1
            except Exception as e:
                self.logger.warning(f"Pending trade kontrolu hata satir {idx}: {e}")

        if success or fail:
            self.logger.info(
                f"Pending kontrol: {success} basarili, {fail} basarisiz kapatildi"
            )
        return df, success, fail
