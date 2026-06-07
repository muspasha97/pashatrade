"""
state.py
--------
Stateless GitHub Actions ortaminda durum yonetimi. Onceki dongunun order book,
OI ve likidasyon proxy verilerini JSON olarak repo'da saklar. Bu sayede
spoofing tespiti ve OI degisim hesabi mumkun olur.

Klasor yapisi:
  state/
    ob_BTCUSDT.json       # son OB snapshot (her dongude guncellenir)
    oi_BTCUSDT.json       # OI tarihce (rolling 4 saat)
    liq_BTCUSDT.json      # likidasyon proxy event log (rolling 4 saat)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src import config
from src.utils import (
    atomic_write_json,
    now_utc,
    now_utc_iso,
    safe_read_json,
    safe_pct_change,
    setup_logger,
    to_binance_symbol,
)


logger = setup_logger("state")


# ---------- VERI YAPILARI ----------
@dataclass
class OBSnapshot:
    """Order book snapshot - state'te tutulan minimum bilgi."""
    timestamp: str
    bids: list[list[float]]  # [[price, amount], ...]
    asks: list[list[float]]
    mid_price: float


@dataclass
class SpoofingResult:
    """Spoofing tespit sonucu."""
    bid_spoofed: bool = False         # buyuk bid duvari yok oldu
    ask_spoofed: bool = False         # buyuk ask duvari yok oldu
    cancelled_bid_size: float = 0.0   # iptal edilen bid hacmi
    cancelled_ask_size: float = 0.0   # iptal edilen ask hacmi
    spoof_ratio: float = 0.0          # son 1 saat icinde tespit edilen / toplam


# ---------- ANA STATE MANAGER ----------
class StateManager:
    """
    State dosyalarini okur/yazar. Hesaplama yapmaz, sadece persistence.
    Spoofing ve OI degisim hesabi state'i kullanarak yapilir.
    """

    def __init__(self) -> None:
        self.logger = logger

    # ============= ORDER BOOK SNAPSHOT =============
    def _ob_path(self, symbol: str):
        return config.STATE_DIR / f"ob_{to_binance_symbol(symbol)}.json"

    def save_ob_snapshot(
        self, symbol: str, order_book: dict[str, Any]
    ) -> None:
        """
        Order book'un yalin temsilini diske yazar. Top 100 bid+ask saklanir
        (tum derinligi tutmaya gerek yok, state buyumesin).
        """
        if not order_book or "bids" not in order_book:
            return

        bids = order_book.get("bids", [])[:100]
        asks = order_book.get("asks", [])[:100]
        if not bids or not asks:
            return

        mid = (bids[0][0] + asks[0][0]) / 2.0
        snap = OBSnapshot(
            timestamp=now_utc_iso(),
            bids=[[float(b[0]), float(b[1])] for b in bids],
            asks=[[float(a[0]), float(a[1])] for a in asks],
            mid_price=mid,
        )
        try:
            atomic_write_json(self._ob_path(symbol), snap.__dict__)
        except Exception as e:
            self.logger.error(f"OB snapshot kaydedilemedi {symbol}: {e}")

    def load_ob_snapshot(self, symbol: str) -> OBSnapshot | None:
        """Onceki dongunun OB snapshot'ini yukler. Ilk dongu icin None."""
        data = safe_read_json(self._ob_path(symbol))
        if not data:
            return None
        try:
            return OBSnapshot(
                timestamp=data["timestamp"],
                bids=data["bids"],
                asks=data["asks"],
                mid_price=data["mid_price"],
            )
        except (KeyError, TypeError) as e:
            self.logger.warning(f"OB snapshot bozuk {symbol}: {e}")
            return None

    # ============= SPOOFING TESPIT =============
    def detect_spoofing(
        self,
        symbol: str,
        current_ob: dict[str, Any],
        recent_trades: list[dict[str, Any]],
    ) -> SpoofingResult:
        """
        Spoofing mantigi:
        1. Onceki snapshot'taki en buyuk bid/ask duvarini bul
        2. Anlik OB'de bu fiyat seviyesinde ayni buyukluk var mi?
        3. Yoksa ve son 15 dakikada bu fiyatta trade GOZLENMEDIYSE = spoof
           (Trade gozlenseydi, duvar dolmus olurdu, bu normal.)
        """
        result = SpoofingResult()
        prev = self.load_ob_snapshot(symbol)
        if prev is None or not current_ob.get("bids") or not current_ob.get("asks"):
            return result

        # Onceki en buyuk bid duvarini bul
        prev_bids = prev.bids
        prev_asks = prev.asks
        if not prev_bids or not prev_asks:
            return result

        max_bid_idx = max(range(len(prev_bids)), key=lambda i: prev_bids[i][1])
        max_ask_idx = max(range(len(prev_asks)), key=lambda i: prev_asks[i][1])
        prev_max_bid = prev_bids[max_bid_idx]  # [price, amount]
        prev_max_ask = prev_asks[max_ask_idx]

        # Onceki ortalama miktar - duvar mi degil mi karari
        avg_bid_size = sum(b[1] for b in prev_bids[:10]) / 10.0
        avg_ask_size = sum(a[1] for a in prev_asks[:10]) / 10.0

        # Trade fiyatlari (son 15 dk)
        trade_prices = [t["price"] for t in recent_trades if "price" in t]

        # ----- BID DUVARI KONTROLU -----
        if prev_max_bid[1] > avg_bid_size * config.WALL_DETECT_MULTIPLIER:
            wall_price = prev_max_bid[0]
            wall_size = prev_max_bid[1]
            tolerance = wall_price * config.SPOOFING_PRICE_TOLERANCE_PCT / 100.0

            # Bu fiyat seviyesinde anlik OB'de buyuk emir var mi?
            cur_match = False
            for b in current_ob["bids"][:50]:
                if abs(b[0] - wall_price) <= tolerance:
                    if b[1] >= wall_size * 0.5:  # en az yarisi hala duruyorsa
                        cur_match = True
                        break

            # Bu fiyat civarinda trade gerceklesti mi?
            traded_through = any(
                abs(p - wall_price) <= tolerance for p in trade_prices
            )

            if not cur_match and not traded_through:
                result.bid_spoofed = True
                result.cancelled_bid_size = wall_size

        # ----- ASK DUVARI KONTROLU -----
        if prev_max_ask[1] > avg_ask_size * config.WALL_DETECT_MULTIPLIER:
            wall_price = prev_max_ask[0]
            wall_size = prev_max_ask[1]
            tolerance = wall_price * config.SPOOFING_PRICE_TOLERANCE_PCT / 100.0

            cur_match = False
            for a in current_ob["asks"][:50]:
                if abs(a[0] - wall_price) <= tolerance:
                    if a[1] >= wall_size * 0.5:
                        cur_match = True
                        break

            traded_through = any(
                abs(p - wall_price) <= tolerance for p in trade_prices
            )

            if not cur_match and not traded_through:
                result.ask_spoofed = True
                result.cancelled_ask_size = wall_size

        # ----- ROLLING SPOOF ORANI -----
        # Son 1 saatin spoof gecmisi (basit log)
        spoof_log_path = config.STATE_DIR / f"spoof_{to_binance_symbol(symbol)}.json"
        log_data = safe_read_json(spoof_log_path, default=[])
        if result.bid_spoofed or result.ask_spoofed:
            log_data.append({
                "timestamp": now_utc_iso(),
                "bid_spoof": result.bid_spoofed,
                "ask_spoof": result.ask_spoofed,
            })

        # Sadece son 4 saati tut
        cutoff = now_utc().timestamp() - 4 * 3600
        try:
            from datetime import datetime, timezone
            log_data = [
                e for e in log_data
                if datetime.fromisoformat(e["timestamp"].replace("Z", "+00:00")).timestamp() >= cutoff
            ]
        except Exception:
            log_data = log_data[-50:]  # fallback: son 50 kayit

        try:
            atomic_write_json(spoof_log_path, log_data)
        except Exception as e:
            self.logger.warning(f"Spoof log yazilamadi: {e}")

        # Spoofing orani: son 1 saatte spoof / toplam dongu (~4)
        recent_spoofs = sum(
            1 for e in log_data
            if e.get("bid_spoof") or e.get("ask_spoof")
        )
        # 4 saat icinde ~16 dongu - bu donguden gelen orani normalize et
        result.spoof_ratio = min(1.0, recent_spoofs / 16.0)

        return result

    # ============= OI TARIHCESI =============
    def _oi_path(self, symbol: str):
        return config.STATE_DIR / f"oi_{to_binance_symbol(symbol)}.json"

    def save_oi_value(self, symbol: str, oi_value: float) -> None:
        """
        OI degerini tarihceye ekle. Son 4 saatin verisini tut (~16 nokta).
        """
        path = self._oi_path(symbol)
        history = safe_read_json(path, default=[])

        history.append({
            "timestamp": now_utc_iso(),
            "oi": float(oi_value),
        })

        # Son 4 saati tut
        cutoff = now_utc().timestamp() - 4 * 3600
        try:
            from datetime import datetime
            history = [
                h for h in history
                if datetime.fromisoformat(h["timestamp"].replace("Z", "+00:00")).timestamp() >= cutoff
            ]
        except Exception:
            history = history[-20:]

        try:
            atomic_write_json(path, history)
        except Exception as e:
            self.logger.error(f"OI tarihce kaydedilemedi {symbol}: {e}")

    def get_oi_change(self, symbol: str, period_minutes: int = 15) -> float:
        """
        Belirli zaman onceki OI'a gore yuzde degisim.
        period_minutes: 5, 15, 60 gibi.
        Eslestirme: en yakin timestamp.
        """
        history = safe_read_json(self._oi_path(symbol), default=[])
        if len(history) < 2:
            return 0.0

        from datetime import datetime
        try:
            current_ts = datetime.fromisoformat(
                history[-1]["timestamp"].replace("Z", "+00:00")
            ).timestamp()
            target_ts = current_ts - period_minutes * 60

            # En yakin gecmis nokta
            best = None
            best_diff = float("inf")
            for h in history[:-1]:
                ts = datetime.fromisoformat(
                    h["timestamp"].replace("Z", "+00:00")
                ).timestamp()
                diff = abs(ts - target_ts)
                if diff < best_diff:
                    best_diff = diff
                    best = h

            if best is None:
                return 0.0
            return safe_pct_change(history[-1]["oi"], best["oi"])
        except Exception as e:
            self.logger.warning(f"OI change hesaplanamadi {symbol}: {e}")
            return 0.0

    # ============= LIKIDASYON PROXY =============
    def _liq_path(self, symbol: str):
        return config.STATE_DIR / f"liq_{to_binance_symbol(symbol)}.json"

    def update_liquidation_proxy(
        self,
        symbol: str,
        oi_change_5m: float,
        price_change_5m: float,
    ) -> dict[str, float]:
        """
        WebSocket olmadigi icin proxy hesabi:
          - OI 5dk'da > %2 dustu VE
          - fiyat ayni 5dk'da ayni yonde > %0.5 hareket etti
        => buyuk olasilikla likidasyon (long ya da short kaskadi)

        Long likidasyon: OI duser, fiyat duser (pozisyonlar zorla satilir)
        Short likidasyon: OI duser, fiyat yukselir (pozisyonlar zorla kapanir)
        """
        if not config.ENABLE_LIQUIDATION:
            return {"long_1h": 0.0, "short_1h": 0.0, "total_1h": 0.0}

        path = self._liq_path(symbol)
        log = safe_read_json(path, default=[])

        oi_dropped = oi_change_5m < -config.LIQ_PROXY_OI_DROP_PCT
        new_event = None

        if oi_dropped and price_change_5m < -config.LIQ_PROXY_PRICE_MOVE_PCT:
            # Long likidasyon proxy
            magnitude = abs(oi_change_5m) * abs(price_change_5m) * 1_000_000
            new_event = {
                "timestamp": now_utc_iso(),
                "side": "long",
                "magnitude": magnitude,
            }
        elif oi_dropped and price_change_5m > config.LIQ_PROXY_PRICE_MOVE_PCT:
            # Short likidasyon proxy
            magnitude = abs(oi_change_5m) * abs(price_change_5m) * 1_000_000
            new_event = {
                "timestamp": now_utc_iso(),
                "side": "short",
                "magnitude": magnitude,
            }

        if new_event:
            log.append(new_event)
            self.logger.info(f"{symbol}: likidasyon proxy {new_event['side']} eventi")

        # Son 4 saati tut
        cutoff_1h = now_utc().timestamp() - 3600
        cutoff_4h = now_utc().timestamp() - 4 * 3600

        from datetime import datetime
        try:
            log = [
                e for e in log
                if datetime.fromisoformat(e["timestamp"].replace("Z", "+00:00")).timestamp() >= cutoff_4h
            ]
            long_1h = sum(
                e["magnitude"] for e in log
                if e["side"] == "long" and
                datetime.fromisoformat(e["timestamp"].replace("Z", "+00:00")).timestamp() >= cutoff_1h
            )
            short_1h = sum(
                e["magnitude"] for e in log
                if e["side"] == "short" and
                datetime.fromisoformat(e["timestamp"].replace("Z", "+00:00")).timestamp() >= cutoff_1h
            )
        except Exception:
            long_1h, short_1h = 0.0, 0.0

        try:
            atomic_write_json(path, log)
        except Exception as e:
            self.logger.warning(f"Liq log yazilamadi {symbol}: {e}")

        return {
            "long_1h": long_1h,
            "short_1h": short_1h,
            "total_1h": long_1h + short_1h,
            "net_1h": short_1h - long_1h,  # pozitif: short squeeze sonrasi
        }

    # ============= MAKRO CACHE (saatlik) =============
    def _macro_cache_path(self):
        return config.STATE_DIR / "macro_cache.json"

    def get_cached_macro(self) -> dict[str, Any] | None:
        """
        Makro veri cache'i. 1 saatten yeni ise cache doner, degilse None.
        Bu sayede dongu basina F&G/CoinGecko cagrisi gereksiz olmaz.
        """
        data = safe_read_json(self._macro_cache_path())
        if not data:
            return None
        try:
            from datetime import datetime
            cached_at = datetime.fromisoformat(
                data["cached_at"].replace("Z", "+00:00")
            ).timestamp()
            age_sec = now_utc().timestamp() - cached_at
            if age_sec < 3600:  # 1 saatten yeni
                return data
        except Exception:
            pass
        return None

    def save_macro_cache(self, macro_dict: dict[str, Any]) -> None:
        macro_dict["cached_at"] = now_utc_iso()
        try:
            atomic_write_json(self._macro_cache_path(), macro_dict)
        except Exception as e:
            self.logger.error(f"Macro cache kaydedilemedi: {e}")
