"""
main.py
-------
Ana orchestrator. Her 15 dakikada cron tarafindan tetiklenir.

Dongu adımlari:
1. Initialize tum bilesenler
2. Makro veri (cache'li) cek - F&G, BTC dominance
3. BTC referans verisini cek (tum altcoinler icin gerekli)
4. Her grup icin:
   - Excel'i yukle
   - Her coin icin:
     * Veri topla (SymbolBundle)
     * Indikator hesapla
     * Mikroyapi hesapla
     * State manager - OI history kaydet, spoofing tespit, liq proxy
     * Feature dict olustur
     * Bekleyen islemleri kontrol et (TP/SL)
     * Karar uret
     * Sinyal varsa yeni islem ac
     * Satiri Excel'e ekle
   - Excel'i atomik yaz
5. Loglari yaz, isi bitir
"""
from __future__ import annotations

import sys
import traceback
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from src import config
from src.collector import DataCollector, MacroBundle, SymbolBundle
from src.decision import DecisionEngine
from src.excel_io import GroupExcel
from src.indicators import IndicatorCalculator
from src.microstructure import MicrostructureAnalyzer
from src.state import StateManager
from src.trader import PaperTrader
from src.utils import (
    market_session,
    now_utc_iso,
    safe_pct_change,
    setup_logger,
)


logger = setup_logger("main")


# ============= BTC TREND ETIKETI HAZIRLA =============
def build_btc_reference_features(
    btc_bundle: SymbolBundle, indicators: IndicatorCalculator
) -> dict[str, Any]:
    """BTC referans verisi - tum altcoin satirlarinda kullanilir."""
    out = {
        "btc_trendi": "yatay",
        "btc_pct_1h": 0.0,
        "btc_pct_15m": 0.0,
    }
    try:
        ohlcv_1h = btc_bundle.ohlcv.get("1h", [])
        out["btc_trendi"] = indicators.compute_trend(ohlcv_1h)
        if ohlcv_1h and len(ohlcv_1h) >= 2:
            out["btc_pct_1h"] = safe_pct_change(ohlcv_1h[-1][4], ohlcv_1h[-2][4])
        ohlcv_15m = btc_bundle.ohlcv.get("15m", [])
        if ohlcv_15m and len(ohlcv_15m) >= 2:
            out["btc_pct_15m"] = safe_pct_change(ohlcv_15m[-1][4], ohlcv_15m[-2][4])
    except Exception as e:
        logger.warning(f"BTC ref hata: {e}")
    return out


# ============= FEATURE DICT BUILDER =============
def build_features(
    coin: str,
    bundle: SymbolBundle,
    btc_ref: dict[str, Any],
    macro: MacroBundle,
    indicators: IndicatorCalculator,
    microstructure: MicrostructureAnalyzer,
    state: StateManager,
) -> dict[str, Any]:
    """
    Bundle ve referans verilerden tek bir feature dict olusturur.
    Excel'in tum sutunlarini doldurur.
    """
    f: dict[str, Any] = {}
    now = now_utc_iso()

    # --- META ---
    f["tarih"] = now
    f["seans"] = market_session()
    f["errors_count"] = len(bundle.errors)

    # --- BTC REFERANS ---
    f.update(btc_ref)

    # --- MAKRO ---
    f["korku_endeksi"] = macro.fng_current
    f["fng_7d_avg"] = macro.fng_7d_avg
    f["btc_dominance"] = macro.btc_dominance

    # --- OHLC TURETIMLERI ---
    ohlcv_1h = bundle.ohlcv.get("1h", [])
    ohlcv_15m = bundle.ohlcv.get("15m", [])
    ohlcv_5m = bundle.ohlcv.get("5m", [])
    ohlcv_4h = bundle.ohlcv.get("4h", [])
    ohlcv_1d = bundle.ohlcv.get("1d", [])

    df_15m = indicators.ohlcv_to_df(ohlcv_15m)
    df_1h = indicators.ohlcv_to_df(ohlcv_1h)
    df_5m = indicators.ohlcv_to_df(ohlcv_5m)

    # Anlik fiyat - ticker veya order book mid
    current_price = 0.0
    if bundle.ticker and "last" in bundle.ticker and bundle.ticker["last"]:
        current_price = float(bundle.ticker["last"])
    elif bundle.order_book:
        current_price = microstructure.compute_mid_price(bundle.order_book)
    elif not df_15m.empty:
        current_price = float(df_15m["close"].iloc[-1])

    f["coin_trend_15m"] = indicators.compute_trend(ohlcv_15m)
    f["coin_trend_1h"] = indicators.compute_trend(ohlcv_1h)

    f["pct_change_15m"] = indicators.compute_pct_change(df_15m, 1)
    f["pct_change_1h"] = indicators.compute_pct_change(df_1h, 1)
    if bundle.ticker:
        f["pct_change_24h"] = float(bundle.ticker.get("percentage", 0.0) or 0.0)
    else:
        f["pct_change_24h"] = 0.0

    # --- TEKNIK INDIKATORLER ---
    f["rsi_degeri"] = indicators.compute_rsi(df_15m, 14)
    f["rsi_5m"] = indicators.compute_rsi(indicators.ohlcv_to_df(ohlcv_5m), 14)
    f["rsi_1h"] = indicators.compute_rsi(df_1h, 14)

    macd_15m = indicators.compute_macd(df_15m)
    f["macd_hist_15m"] = macd_15m["hist"]

    bb_15m = indicators.compute_bbands(df_15m)
    f["bb_pct_b_15m"] = bb_15m["pct_b"]
    f["bb_width_15m"] = bb_15m["width"]

    atr_15m = indicators.compute_atr(df_15m, 14)
    f["atr_kullanildi"] = atr_15m  # trader bunu okuyacak
    f["atr_pct_15m"] = indicators.compute_atr_pct(df_15m, 14)

    # --- VWAP ---
    # Gunluk VWAP icin 5m bar yeterli (~288 bar yaklasik 24 saat)
    vwap = indicators.compute_vwap(df_5m)
    f["vwap_daily"] = vwap
    f["vwap_sapmasi"] = indicators.compute_vwap_deviation(current_price, vwap)

    # --- VOLUME PROFILE (POC) ---
    vp = indicators.compute_volume_profile(df_5m, num_bins=50)
    f["poc_24h"] = vp["poc"]
    f["vah_24h"] = vp["vah"]
    f["val_24h"] = vp["val"]
    f["poc_uzakligi"] = indicators.compute_poc_distance(current_price, vp["poc"])

    # --- REALIZED VOL ---
    # 1h DataFrame ile - 24 saatlik veri
    f["realized_vol_24h"] = indicators.compute_realized_vol(df_1h, 24)

    # --- ORDER BOOK ---
    if bundle.order_book:
        f["tahta_al_sat_orani"] = microstructure.compute_imbalance(bundle.order_book, 1.0)
        f["imb_0p5"] = microstructure.compute_imbalance(bundle.order_book, 0.5)
        f["imb_2p0"] = microstructure.compute_imbalance(bundle.order_book, 2.0)
        f["spread_bps"] = microstructure.compute_spread_bps(bundle.order_book)

        walls = microstructure.find_largest_walls(bundle.order_book)
        f["bid_wall_size"] = walls["bid_wall_size"]
        f["ask_wall_size"] = walls["ask_wall_size"]
        f["bid_wall_dist_pct"] = walls["bid_wall_dist_pct"]
        f["ask_wall_dist_pct"] = walls["ask_wall_dist_pct"]
        f["largest_wall_side"] = walls["largest_wall_side"]
        f["duvar_mesafesi"] = walls["largest_wall_dist_pct"]

        # Spoofing tespiti (state manager)
        spoof = state.detect_spoofing(coin, bundle.order_book, bundle.trades)
        f["iptal_edilen_duvar_orani"] = spoof.spoof_ratio

        # Sonraki dongu icin snapshot kaydet
        state.save_ob_snapshot(coin, bundle.order_book)
    else:
        f.update({
            "tahta_al_sat_orani": 1.0, "imb_0p5": 1.0, "imb_2p0": 1.0,
            "spread_bps": 0.0, "bid_wall_size": 0.0, "ask_wall_size": 0.0,
            "bid_wall_dist_pct": 0.0, "ask_wall_dist_pct": 0.0,
            "largest_wall_side": "yok", "duvar_mesafesi": 0.0,
            "iptal_edilen_duvar_orani": 0.0,
        })

    # --- TRADE FLOW ---
    if bundle.trades:
        f["cvd_miktari"] = microstructure.compute_cvd(bundle.trades, 15)
        whales = microstructure.count_large_trades(bundle.trades, 50000.0, 15)
        f["whale_count_15m"] = whales["count"]
        f["whale_net_flow_15m"] = whales["net_flow"]
    else:
        f["cvd_miktari"] = 0.0
        f["whale_count_15m"] = 0.0
        f["whale_net_flow_15m"] = 0.0

    # --- BTC GORECELI GUC ---
    f["btc_korelasyonu"] = microstructure.compute_relative_strength(
        f["pct_change_1h"], btc_ref["btc_pct_1h"]
    )

    # --- OPEN INTEREST ---
    if bundle.open_interest:
        try:
            oi_val = float(
                bundle.open_interest.get("openInterestAmount")
                or bundle.open_interest.get("openInterestValue")
                or 0.0
            )
            f["open_interest_usd"] = oi_val * current_price if current_price else oi_val
            state.save_oi_value(coin, oi_val)
            f["oi_degisimi"] = state.get_oi_change(coin, period_minutes=60)
            oi_change_5m = state.get_oi_change(coin, period_minutes=5)
        except Exception as e:
            logger.warning(f"{coin} OI hata: {e}")
            f["open_interest_usd"] = 0.0
            f["oi_degisimi"] = 0.0
            oi_change_5m = 0.0
    else:
        f["open_interest_usd"] = 0.0
        f["oi_degisimi"] = 0.0
        oi_change_5m = 0.0

    # --- FUNDING ---
    if bundle.funding_rate:
        try:
            f["funding_oran"] = float(bundle.funding_rate.get("fundingRate", 0.0) or 0.0)
        except Exception:
            f["funding_oran"] = 0.0
    else:
        f["funding_oran"] = 0.0

    if bundle.funding_history:
        try:
            recent_3 = bundle.funding_history[-3:]
            avg = sum(float(h.get("fundingRate", 0.0) or 0.0) for h in recent_3) / max(len(recent_3), 1)
            f["funding_24h_avg"] = avg
        except Exception:
            f["funding_24h_avg"] = f["funding_oran"]
    else:
        f["funding_24h_avg"] = f["funding_oran"]

    # --- FUTURES DATA ENDPOINTS ---
    f["top_trader_ls_pos"] = _extract_ratio(bundle.top_lspos_ratio, "longShortRatio")
    f["global_ls_ratio"] = _extract_ratio(bundle.global_ls_ratio, "longShortRatio")
    f["taker_ratio_5m"] = _extract_ratio(bundle.taker_ratio_5m, "buySellRatio")
    f["taker_ratio_15m"] = _extract_ratio(bundle.taker_ratio_15m, "buySellRatio")

    # --- LIKIDASYON PROXY ---
    price_change_5m = indicators.compute_pct_change(df_5m, 1) if not df_5m.empty else 0.0
    liq = state.update_liquidation_proxy(coin, oi_change_5m, price_change_5m)
    f["likidasyon_miktari"] = liq["total_1h"]

    return f


def _extract_ratio(data: list[dict], key: str) -> float:
    """Futures-data response'undan numeric oran cek."""
    if not data:
        return 0.0
    try:
        return float(data[-1].get(key, 0.0) or 0.0)
    except (ValueError, TypeError):
        return 0.0


# ============= ANA DONGU =============
def run_cycle() -> int:
    """
    Tek bir donguyu calistirir.
    Return: 0 basarili, 1 hata
    """
    cycle_start = datetime.now(timezone.utc)
    logger.info(f"========== DONGU BASLADI {cycle_start.isoformat()} ==========")

    try:
        # ----- INIT -----
        collector = DataCollector()
        indicators = IndicatorCalculator()
        microstructure = MicrostructureAnalyzer()
        state = StateManager()
        trader = PaperTrader()
        decision_engine = DecisionEngine()  # red set kurallarini RULES_DIR'den okur

        # ----- MAKRO (cache'li) -----
        cached_macro_dict = state.get_cached_macro()
        if cached_macro_dict:
            macro = MacroBundle(
                fng_current=cached_macro_dict.get("fng_current", 50.0),
                fng_7d_avg=cached_macro_dict.get("fng_7d_avg", 50.0),
                btc_dominance=cached_macro_dict.get("btc_dominance", 0.0),
                usdt_dominance=cached_macro_dict.get("usdt_dominance", 0.0),
                total_mc_change_24h=cached_macro_dict.get("total_mc_change_24h", 0.0),
            )
            logger.info("Makro veri cache'ten okundu")
        else:
            macro = collector.collect_macro()
            state.save_macro_cache({
                "fng_current": macro.fng_current,
                "fng_7d_avg": macro.fng_7d_avg,
                "btc_dominance": macro.btc_dominance,
                "usdt_dominance": macro.usdt_dominance,
                "total_mc_change_24h": macro.total_mc_change_24h,
            })

        # ----- BTC REFERANS -----
        btc_bundle = collector.collect_btc_reference()
        btc_ref = build_btc_reference_features(btc_bundle, indicators)
        logger.info(
            f"BTC ref: trend={btc_ref['btc_trendi']} "
            f"1h={btc_ref['btc_pct_1h']:.2f}%"
        )

        # ----- HER GRUP ICIN -----
        total_new = 0
        total_success = 0
        total_fail = 0

        for group, coins in config.COIN_GROUPS.items():
            logger.info(f"--- Grup: {group} ({len(coins)} coin) ---")
            gex = GroupExcel(group)
            gex.load()

            for coin in coins:
                try:
                    # BTC bundle'i tekrar cekme - cache
                    if coin == "BTC/USDT":
                        bundle = btc_bundle
                    else:
                        bundle = collector.collect_for_symbol(coin)

                    # ----- FEATURE DICT -----
                    features = build_features(
                        coin, bundle, btc_ref, macro,
                        indicators, microstructure, state,
                    )

                    # ----- BEKLEYEN ISLEMLER -----
                    df = gex.get_df(coin)
                    current_price = features.get("giris_fiyati_anlik", 0.0)
                    if current_price == 0.0:
                        current_price = (
                            float(bundle.ticker.get("last", 0.0) or 0.0)
                            if bundle.ticker else
                            microstructure.compute_mid_price(bundle.order_book)
                        )

                    if current_price > 0:
                        df, succ, fail = trader.check_pending_trades(df, current_price)
                        gex.set_df(coin, df)
                        total_success += succ
                        total_fail += fail

                    # ----- KARAR -----
                    decision = decision_engine.predict(features)

                    # ----- YENI ISLEM (varsa) -----
                    new_row_extras = {}
                    if decision["direction"] != "neutral" and current_price > 0:
                        atr = features.get("atr_kullanildi", 0.0)
                        new_row_extras = trader.open_trade(
                            coin, decision, current_price, atr, features
                        )
                        if new_row_extras:
                            total_new += 1

                    # ----- HER DONGUDE BIR SATIR EKLE -----
                    # Sinyal olmasa bile veri toplama amaciyla satir ekleniyor.
                    # Bu, geriye donuk pattern analizini guclendirir.
                    row = dict(features)
                    if new_row_extras:
                        # Yeni islem aciliyorsa entry/TP/SL bilgilerini de ekle
                        row.update({
                            "tahmin_yonu": new_row_extras["tahmin_yonu"],
                            "giris_fiyati": new_row_extras["giris_fiyati"],
                            "hedef_fiyat": new_row_extras["hedef_fiyat"],
                            "stop_fiyati": new_row_extras["stop_fiyati"],
                            "islem_durumu": "bekliyor",
                            "kirmizi_set_etiketi": new_row_extras["kirmizi_set_etiketi"],
                            "skor": new_row_extras["skor"],
                            "confidence": new_row_extras["confidence"],
                            "pozisyon_buyukluk_usd": new_row_extras["pozisyon_buyukluk_usd"],
                        })
                    else:
                        # Neutral karar - satir yine de eklenir ama islem yok
                        row.update({
                            "tahmin_yonu": decision["direction"],
                            "giris_fiyati": current_price,
                            "hedef_fiyat": pd.NA,
                            "stop_fiyati": pd.NA,
                            "islem_durumu": "izleniyor",
                            "kirmizi_set_etiketi": "",
                            "skor": decision["score"],
                            "confidence": decision["confidence"],
                        })

                    gex.append_row(coin, row)

                except Exception as e:
                    logger.error(
                        f"{coin} dongusu hata: {type(e).__name__}: {e}\n"
                        f"{traceback.format_exc()}"
                    )

            # Grup Excel'ini yaz
            gex.write_atomic()

        # ----- OZET -----
        elapsed = (datetime.now(timezone.utc) - cycle_start).total_seconds()
        logger.info(
            f"========== DONGU TAMAMLANDI {elapsed:.1f}s | "
            f"yeni islem={total_new} | basarili kapatma={total_success} | "
            f"basarisiz kapatma={total_fail} =========="
        )
        return 0

    except Exception as e:
        logger.error(
            f"Donguda kritik hata: {type(e).__name__}: {e}\n{traceback.format_exc()}"
        )
        return 1


if __name__ == "__main__":
    sys.exit(run_cycle())
