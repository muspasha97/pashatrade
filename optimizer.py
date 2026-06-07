"""
optimizer.py
------------
Haftalik calisan pattern detection. Tum Excel dosyalarini okur, kapanmis
islemleri toplar, scikit-learn yerine basit ama saglam pandas-tabanli
binning + Bayesian count ile basarili feature kombinasyonlarini cikarir.

Kural cikarma:
1. Sayisal feature'lar 3 quantile'a (low/mid/high) bolunur.
2. Her 2-3'lu kombinasyon icin hit rate hesabi.
3. Min count = 3, min hit rate = 0.70 -> kirmizi set'e ekle.
4. basarili_kurallar.json'a yaz.

Cron: pazar gecesi 03:00 UTC (haftada 1 kez).
"""
from __future__ import annotations

import itertools
import json
from pathlib import Path
from typing import Any

import pandas as pd

from src import config
from src.utils import atomic_write_json, now_utc_iso, setup_logger, to_sheet_name


logger = setup_logger("optimizer")


# ============= PARAMETRELER =============
MIN_TRADES_FOR_DETECTION = 30  # Bir coinde toplam islem
MIN_COMBO_COUNT = 3            # Bir kombinasyon en az kac islemde gozulmeli
MIN_HIT_RATE = 0.70            # %70+ basari
MAX_COMBO_SIZE = 3             # Kombinasyon en fazla 3 feature
NUM_BINS = 3                   # low/mid/high

# Hangi feature'lar pattern detection'a girer?
# Tum feature'lar degil - en cok prediktif olanlar
ANALYSIS_FEATURES = [
    "rsi_degeri",
    "tahta_al_sat_orani",
    "cvd_miktari",
    "oi_degisimi",
    "funding_oran",
    "duvar_mesafesi",
    "btc_korelasyonu",
    "poc_uzakligi",
    "vwap_sapmasi",
    "korku_endeksi",
    "imb_0p5",
    "imb_2p0",
    "macd_hist_15m",
    "bb_pct_b_15m",
    "atr_pct_15m",
    "whale_net_flow_15m",
    "top_trader_ls_pos",
    "global_ls_ratio",
    "taker_ratio_15m",
    "btc_pct_1h",
    "fng_7d_avg",
]


# Kategorik feature'lar - aynen kullanilir, binning yapilmaz
CATEGORICAL_FEATURES = [
    "btc_trendi",
    "coin_trend_15m",
    "coin_trend_1h",
    "seans",
    "largest_wall_side",
]


def discretize(series: pd.Series, num_bins: int = 3) -> pd.Series:
    """
    Sayisal seriyi 'low/mid/high' kategorisine bol.
    Quantile-tabanli (esit dolu kovalar).
    """
    try:
        return pd.qcut(
            series, q=num_bins,
            labels=["low", "mid", "high"][:num_bins],
            duplicates="drop",
        )
    except (ValueError, IndexError):
        # Tum degerler ayniysa qcut basarisiz olur
        return pd.Series(["mid"] * len(series), index=series.index)


def load_all_closed_trades() -> pd.DataFrame:
    """
    Tum gruplarin Excel'lerinden kapanmis islemleri toplar.
    Donus: tek bir DataFrame, ek 'coin' sutunu ile.
    """
    all_trades = []
    for group, coins in config.COIN_GROUPS.items():
        path = config.excel_path_for_group(group)
        if not path.exists():
            logger.info(f"Excel yok, atlaniyor: {path.name}")
            continue

        try:
            xl = pd.ExcelFile(path, engine="openpyxl")
            for coin in coins:
                sheet = to_sheet_name(coin)
                if sheet not in xl.sheet_names:
                    continue
                df = pd.read_excel(xl, sheet_name=sheet)
                if df.empty:
                    continue
                # Sadece kapanmis islemler
                df = df[df["islem_durumu"].isin(["basarili", "basarisiz"])]
                if df.empty:
                    continue
                df = df.copy()
                df["coin"] = coin
                df["group"] = group
                all_trades.append(df)
        except Exception as e:
            logger.error(f"Excel okuma hatasi {path.name}: {e}")

    if not all_trades:
        return pd.DataFrame()
    return pd.concat(all_trades, ignore_index=True)


def find_red_sets(df: pd.DataFrame) -> list[dict[str, Any]]:
    """
    DataFrame'den basarili kombinasyonlari cikarir.
    Her coin icin AYRI analiz - cunku farkli coinler farkli kombinasyonlara
    tepki verir.
    """
    if df.empty:
        return []

    all_rules: list[dict[str, Any]] = []

    for coin in df["coin"].unique():
        coin_df = df[df["coin"] == coin].copy()
        if len(coin_df) < MIN_TRADES_FOR_DETECTION:
            logger.info(
                f"{coin}: {len(coin_df)} islem - en az {MIN_TRADES_FOR_DETECTION} gerekli, atlaniyor"
            )
            continue

        # Sayisal feature'lari discretize et
        binned_cols = {}
        for feat in ANALYSIS_FEATURES:
            if feat not in coin_df.columns:
                continue
            series = pd.to_numeric(coin_df[feat], errors="coerce")
            if series.isna().sum() > len(series) * 0.5:
                continue
            try:
                binned_cols[feat] = discretize(series.fillna(series.median()), NUM_BINS)
            except Exception:
                continue

        # Kategorikler aynen
        for feat in CATEGORICAL_FEATURES:
            if feat in coin_df.columns:
                binned_cols[feat] = coin_df[feat].astype(str).fillna("yok")

        if not binned_cols:
            continue

        binned_df = pd.DataFrame(binned_cols)
        binned_df["__outcome__"] = (coin_df["islem_durumu"] == "basarili").values

        # Yon ayrimi - long ve short trade'ler farkli pattern'ler
        binned_df["__direction__"] = coin_df["tahmin_yonu"].values

        for direction in ["long", "short"]:
            dir_df = binned_df[binned_df["__direction__"] == direction]
            if len(dir_df) < MIN_TRADES_FOR_DETECTION // 2:
                continue

            # Kombinasyonlari dene (2 ve 3'lu)
            feature_names = [c for c in dir_df.columns if not c.startswith("__")]
            for combo_size in range(2, min(MAX_COMBO_SIZE + 1, len(feature_names) + 1)):
                for combo in itertools.combinations(feature_names, combo_size):
                    rules = _evaluate_combo(dir_df, combo, coin, direction)
                    all_rules.extend(rules)

    # Sirala: hit_rate * sqrt(trade_count) (istatistiksel anlamlilik)
    all_rules.sort(
        key=lambda r: r["hit_rate"] * (r["trade_count"] ** 0.5),
        reverse=True,
    )

    # En iyi top N kurali al (overfitting onleme)
    return all_rules[:50]


def _evaluate_combo(
    df: pd.DataFrame,
    combo: tuple[str, ...],
    coin: str,
    direction: str,
) -> list[dict[str, Any]]:
    """Bir feature kombinasyonunun her deger setini test eder."""
    rules = []
    try:
        # Groupby ile her deger kombinasyonunun istatistigi
        grouped = df.groupby(list(combo))
        for values, sub_df in grouped:
            count = len(sub_df)
            if count < MIN_COMBO_COUNT:
                continue
            success_rate = sub_df["__outcome__"].sum() / count
            if success_rate < MIN_HIT_RATE:
                continue

            # Kural olustur
            if not isinstance(values, tuple):
                values = (values,)

            conditions = []
            for fname, fval in zip(combo, values):
                conditions.append({
                    "feature": fname,
                    "op": "bin_eq",  # discretize sonrasi tam esitlik
                    "value": str(fval),
                })

            rule_name = f"{coin.replace('/', '_')}_{direction}_{'__'.join(combo[:2])}"
            rules.append({
                "name": rule_name,
                "coin": coin,
                "direction": direction,
                "hit_rate": round(float(success_rate), 4),
                "trade_count": int(count),
                "regime": "tum",  # ileride rejim ayrimi eklenebilir
                "conditions": conditions,
                "discovered_at": now_utc_iso(),
            })
    except Exception as e:
        logger.warning(f"Combo eval hata {combo}: {e}")
    return rules


def main() -> int:
    """Optimizer'in entry point'i."""
    logger.info("========== OPTIMIZER BASLADI ==========")

    try:
        df = load_all_closed_trades()
        if df.empty:
            logger.warning("Hic kapanmis islem yok - kural cikarilamiyor")
            return 0

        logger.info(f"Toplam kapanmis islem: {len(df)}")
        logger.info(f"Coin sayisi: {df['coin'].nunique()}")
        logger.info(f"Genel basari orani: {(df['islem_durumu'] == 'basarili').mean():.2%}")

        rules = find_red_sets(df)
        logger.info(f"{len(rules)} kirmizi set kurali bulundu")

        if rules:
            out_path = config.RULES_DIR / "basarili_kurallar.json"
            atomic_write_json(out_path, rules)
            logger.info(f"Kurallar yazildi: {out_path}")

            # Top 5'i logla
            for r in rules[:5]:
                logger.info(
                    f"  -> {r['name']}: hit_rate={r['hit_rate']:.0%}, "
                    f"count={r['trade_count']}"
                )

        return 0
    except Exception as e:
        import traceback
        logger.error(f"Optimizer hata: {e}\n{traceback.format_exc()}")
        return 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
