"""
microstructure.py
-----------------
Order book ve trade flow analizleri.
- Imbalance (tahta al/sat orani)
- Duvar tespiti ve mesafesi
- CVD (Cumulative Volume Delta)
- BTC korelasyon / goreceli guc
"""
from __future__ import annotations

from typing import Any

from src.utils import safe_pct_change, safe_ratio, setup_logger

logger = setup_logger("microstructure")


class MicrostructureAnalyzer:
    """Order book ve trade verisi uzerinden mikroyapi feature'lari."""

    def __init__(self) -> None:
        self.logger = logger

    # ============= IMBALANCE (TAHTA AL/SAT) =============
    def compute_imbalance(
        self, order_book: dict[str, Any], pct: float = 1.0
    ) -> float:
        """
        Anlik fiyatin +/- pct% bandindaki bids/asks hacim orani.
        > 1.0 -> alici baskin
        < 1.0 -> satici baskin
        Kullanicinin istedigi 'tahta_al_sat_orani'.

        pct=1.0 -> ana metrik (kullanicinin orijinal sartnamesi)
        """
        bids = order_book.get("bids", [])
        asks = order_book.get("asks", [])
        if not bids or not asks:
            return 1.0
        try:
            top_bid = bids[0][0]
            top_ask = asks[0][0]
            mid = (top_bid + top_ask) / 2.0
            band_lo = mid * (1.0 - pct / 100.0)
            band_hi = mid * (1.0 + pct / 100.0)

            bid_vol = sum(b[1] for b in bids if b[0] >= band_lo)
            ask_vol = sum(a[1] for a in asks if a[0] <= band_hi)

            return safe_ratio(bid_vol, ask_vol, default=1.0)
        except Exception as e:
            self.logger.warning(f"Imbalance hata: {e}")
            return 1.0

    # ============= DUVAR TESPITI =============
    def find_largest_walls(
        self, order_book: dict[str, Any]
    ) -> dict[str, float]:
        """
        Ilk 100 seviyedeki en buyuk bid ve ask emrini bul.
        Donus:
          - bid_wall_size, bid_wall_price, bid_wall_dist_pct
          - ask_wall_size, ask_wall_price, ask_wall_dist_pct
          - largest_wall_side, largest_wall_dist_pct (kullaniciinin 'duvar_mesafesi')
        """
        result = {
            "bid_wall_size": 0.0, "bid_wall_price": 0.0, "bid_wall_dist_pct": 0.0,
            "ask_wall_size": 0.0, "ask_wall_price": 0.0, "ask_wall_dist_pct": 0.0,
            "largest_wall_side": "yok",
            "largest_wall_dist_pct": 0.0,
        }
        bids = order_book.get("bids", [])[:100]
        asks = order_book.get("asks", [])[:100]
        if not bids or not asks:
            return result

        try:
            mid = (bids[0][0] + asks[0][0]) / 2.0

            # En buyuk bid
            max_bid_idx = max(range(len(bids)), key=lambda i: bids[i][1])
            mb_price, mb_size = bids[max_bid_idx][0], bids[max_bid_idx][1]

            # En buyuk ask
            max_ask_idx = max(range(len(asks)), key=lambda i: asks[i][1])
            ma_price, ma_size = asks[max_ask_idx][0], asks[max_ask_idx][1]

            result["bid_wall_size"] = float(mb_size)
            result["bid_wall_price"] = float(mb_price)
            result["bid_wall_dist_pct"] = (mid - mb_price) / mid * 100.0

            result["ask_wall_size"] = float(ma_size)
            result["ask_wall_price"] = float(ma_price)
            result["ask_wall_dist_pct"] = (ma_price - mid) / mid * 100.0

            # En yakin/buyuk duvar
            if mb_size > ma_size:
                result["largest_wall_side"] = "bid"
                result["largest_wall_dist_pct"] = result["bid_wall_dist_pct"]
            else:
                result["largest_wall_side"] = "ask"
                result["largest_wall_dist_pct"] = result["ask_wall_dist_pct"]

            return result
        except Exception as e:
            self.logger.warning(f"Duvar tespiti hata: {e}")
            return result

    # ============= CVD (Cumulative Volume Delta) =============
    def compute_cvd(
        self, trades: list[dict[str, Any]], minutes: int = 15
    ) -> float:
        """
        Son N dakikadaki market alimlar - market satimlar.
        Pozitif: agresif alici hakim
        Negatif: agresif satici hakim

        Trade structure (ccxt):
          {timestamp: ms, side: 'buy'|'sell', amount: float, price: float}
        """
        if not trades:
            return 0.0
        try:
            cutoff = max(t["timestamp"] for t in trades) - minutes * 60 * 1000
            buy_vol = 0.0
            sell_vol = 0.0
            for t in trades:
                if t["timestamp"] < cutoff:
                    continue
                amount = float(t.get("amount", 0)) * float(t.get("price", 0))
                if t.get("side") == "buy":
                    buy_vol += amount
                elif t.get("side") == "sell":
                    sell_vol += amount
            return buy_vol - sell_vol
        except Exception as e:
            self.logger.warning(f"CVD hata: {e}")
            return 0.0

    # ============= SPREAD =============
    def compute_spread_bps(self, order_book: dict[str, Any]) -> float:
        """Bid-ask spread baz puan cinsinden (10 bps = %0.1)."""
        bids = order_book.get("bids", [])
        asks = order_book.get("asks", [])
        if not bids or not asks:
            return 0.0
        try:
            top_bid = bids[0][0]
            top_ask = asks[0][0]
            mid = (top_bid + top_ask) / 2.0
            return (top_ask - top_bid) / mid * 10000.0
        except Exception:
            return 0.0

    # ============= MID FIYAT =============
    def compute_mid_price(self, order_book: dict[str, Any]) -> float:
        """En iyi bid ve ask ortalamasi."""
        bids = order_book.get("bids", [])
        asks = order_book.get("asks", [])
        if not bids or not asks:
            return 0.0
        return (bids[0][0] + asks[0][0]) / 2.0

    # ============= BTC KORELASYON =============
    def compute_relative_strength(
        self, coin_pct_1h: float, btc_pct_1h: float
    ) -> float:
        """
        Goreceli guc - kullanicinin 'btc_korelasyonu'.
        Pozitif: coin BTC'yi yendi (outperform).
        Negatif: BTC'nin gerisinde kaldi.
        """
        return coin_pct_1h - btc_pct_1h

    # ============= LARGE TRADE / WHALE TESPIT =============
    def count_large_trades(
        self,
        trades: list[dict[str, Any]],
        notional_threshold_usd: float = 50000.0,
        minutes: int = 15,
    ) -> dict[str, float]:
        """Son N dakikadaki balina islemleri (>$X)."""
        if not trades:
            return {"count": 0, "net_flow": 0.0}
        try:
            cutoff = max(t["timestamp"] for t in trades) - minutes * 60 * 1000
            count = 0
            net = 0.0
            for t in trades:
                if t["timestamp"] < cutoff:
                    continue
                notional = float(t.get("amount", 0)) * float(t.get("price", 0))
                if notional >= notional_threshold_usd:
                    count += 1
                    if t.get("side") == "buy":
                        net += notional
                    else:
                        net -= notional
            return {"count": float(count), "net_flow": net}
        except Exception as e:
            self.logger.warning(f"Whale tespiti hata: {e}")
            return {"count": 0, "net_flow": 0.0}

    # ============= DEEP IMBALANCE (KAPSAMLI) =============
    def compute_multi_depth_imbalance(
        self, order_book: dict[str, Any]
    ) -> dict[str, float]:
        """0.5%, 1%, 2% derinlik pencerelerinde imbalance."""
        return {
            "imb_0p5": self.compute_imbalance(order_book, 0.5),
            "imb_1p0": self.compute_imbalance(order_book, 1.0),
            "imb_2p0": self.compute_imbalance(order_book, 2.0),
        }
