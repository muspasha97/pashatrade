"""
decision.py
-----------
Tum feature'lardan tek bir tahmin yonu uretir. Iki mod:
1. AGIRLIKLI SKOR (faz 1-4'te): heuristic agirlik ile tum feature'lar
   tek bir skora (-1..+1) toplanir.
2. KIRMIZI SET (faz 5+'da): basarili_kurallar.json yuklenirse, oncelikle
   kirmizi set kosulu eslesmeye bakilir.
"""
from __future__ import annotations

from typing import Any

from src import config
from src.utils import clamp, safe_read_json, setup_logger

logger = setup_logger("decision")


class DecisionEngine:
    """
    Feature dict'inden tahmin uretir.
    Donus: {direction: long/short/neutral, score, confidence, red_set_tag}
    """

    def __init__(self, red_set_rules: list[dict] | None = None) -> None:
        self.logger = logger
        # Kirmizi set kurallari (optimizer.py ureticisi)
        if red_set_rules is None:
            rules_path = config.RULES_DIR / "basarili_kurallar.json"
            red_set_rules = safe_read_json(rules_path, default=[])
        self.red_set_rules: list[dict] = red_set_rules or []
        if self.red_set_rules:
            self.logger.info(f"{len(self.red_set_rules)} kirmizi set kurali yuklendi")

    # ============= ANA TAHMIN =============
    def predict(self, features: dict[str, Any]) -> dict[str, Any]:
        """
        Feature dict'ten karar uretir.
        Donus:
          - direction: 'long' | 'short' | 'neutral'
          - score: -1..+1 (heuristic)
          - confidence: 0..1
          - red_set_tag: kirmizi set adi varsa, yoksa ""
        """
        # Kirmizi set kontrolu ONCE
        red_tag = self._match_red_set(features)
        if red_tag:
            return {
                "direction": red_tag["direction"],
                "score": 1.0 if red_tag["direction"] == "long" else -1.0,
                "confidence": red_tag.get("hit_rate", 0.75),
                "red_set_tag": red_tag["name"],
            }

        # Heuristic skor
        score = self._compute_heuristic_score(features)
        confidence = min(abs(score) * 1.5, 1.0)

        if score >= config.DECISION_THRESHOLD_LONG:
            direction = "long"
        elif score <= config.DECISION_THRESHOLD_SHORT:
            direction = "short"
        else:
            direction = "neutral"

        return {
            "direction": direction,
            "score": round(score, 4),
            "confidence": round(confidence, 4),
            "red_set_tag": "",
        }

    # ============= HEURISTIC SKOR =============
    def _compute_heuristic_score(self, f: dict[str, Any]) -> float:
        """
        Tum feature'lardan agirlikli skor (-1..+1).
        Her feature kendi normalize edilmis katkisini ekler.

        IMPORTANT: Her feature MUTLAKA dict'te olmali (eksik feature default 0).
        """
        score = 0.0
        weights_total = 0.0

        # --- TREND (BTC + COIN) ---
        # btc_trendi: yukselis=+1, yatay=0, dusus=-1
        btc_trend_map = {"yukselis": 1.0, "yatay": 0.0, "dusus": -1.0}
        coin_trend_15m = btc_trend_map.get(f.get("coin_trend_15m", "yatay"), 0.0)
        btc_trend_1h = btc_trend_map.get(f.get("btc_trendi", "yatay"), 0.0)
        score += coin_trend_15m * 0.15
        score += btc_trend_1h * 0.10
        weights_total += 0.25

        # --- RSI 15m ---
        # <30 oversold (long bias), >70 overbought (short bias)
        rsi = f.get("rsi_degeri", 50.0)
        if rsi < 30:
            score += 0.15 * (30 - rsi) / 30.0
        elif rsi > 70:
            score -= 0.15 * (rsi - 70) / 30.0
        weights_total += 0.15

        # --- IMBALANCE ---
        imb = f.get("tahta_al_sat_orani", 1.0)
        # > 1.5 alici baskin, < 0.67 satici baskin
        if imb > 1.5:
            score += clamp((imb - 1.5) / 1.5, 0, 1) * 0.12
        elif imb < 0.67:
            score -= clamp((0.67 - imb) / 0.67, 0, 1) * 0.12
        weights_total += 0.12

        # --- CVD ---
        cvd = f.get("cvd_miktari", 0.0)
        # Coin'e gore deger buyuk olabilir, sign'i yeterli
        if cvd > 0:
            score += 0.08
        elif cvd < 0:
            score -= 0.08
        weights_total += 0.08

        # --- OI DEGISIM ---
        oi_chg = f.get("oi_degisimi", 0.0)  # 1 saatlik %
        # Trend yonunde OI artisi -> momentum dogrulamasi
        if oi_chg > 2 and score > 0:
            score += 0.08
        elif oi_chg > 2 and score < 0:
            score += 0.04  # OI artiyor ama yon negatif - karisik sinyal
        elif oi_chg < -2:
            score -= 0.05
        weights_total += 0.08

        # --- FUNDING ---
        fr = f.get("funding_oran", 0.0)
        # Asiri pozitif funding -> short bias (kalabalik long)
        if fr > 0.0005:  # %0.05+
            score -= 0.08 * min((fr - 0.0005) / 0.001, 1.0)
        elif fr < -0.0005:
            score += 0.08 * min((-fr - 0.0005) / 0.001, 1.0)
        weights_total += 0.08

        # --- KORKU ENDEKSI ---
        fng = f.get("korku_endeksi", 50.0)
        # Ekstrem korku -> contra long, ekstrem hirs -> contra short
        if fng < 25:
            score += 0.06 * (25 - fng) / 25.0
        elif fng > 75:
            score -= 0.06 * (fng - 75) / 25.0
        weights_total += 0.06

        # --- DUVAR MESAFESI + IPTAL ---
        # Iptal edilen duvar varsa, manipulasyon - nötr et
        spoof_ratio = f.get("iptal_edilen_duvar_orani", 0.0)
        if spoof_ratio > 0.3:
            score *= 0.7  # skoru zayiflat - guvenilmez piyasa
        weights_total += 0.05

        # --- POC UZAKLIGI ---
        poc_dist = f.get("poc_uzakligi", 0.0)
        # POC ustunde kalmis = alici baskin
        if poc_dist > 0.5:
            score += 0.04
        elif poc_dist < -0.5:
            score -= 0.04
        weights_total += 0.04

        # --- VWAP SAPMASI ---
        vwap_dev = f.get("vwap_sapmasi", 0.0)
        # Asiri yukarida -> mean reversion short bias
        if vwap_dev > 1.5:
            score -= 0.04 * min((vwap_dev - 1.5) / 2.0, 1.0)
        elif vwap_dev < -1.5:
            score += 0.04 * min((-vwap_dev - 1.5) / 2.0, 1.0)
        weights_total += 0.04

        # --- BTC GORECELI GUC (sadece altcoinler icin) ---
        rel_strength = f.get("btc_korelasyonu", 0.0)
        if abs(rel_strength) > 0.3:
            score += clamp(rel_strength / 2.0, -0.05, 0.05)
        weights_total += 0.05

        return clamp(score, -1.0, 1.0)

    # ============= KIRMIZI SET ESLEME =============
    def _match_red_set(self, features: dict[str, Any]) -> dict | None:
        """
        Yuklenmis kirmizi set kurallarindan birine eslesirse o kurali doner.
        Kural formati (basarili_kurallar.json):
        {
          "name": "SQUEEZE_PUMP_v1",
          "direction": "long",
          "hit_rate": 0.75,
          "trade_count": 12,
          "regime": "squeeze_breakout",
          "conditions": [
            {"feature": "rsi_degeri", "op": "<", "value": 35},
            {"feature": "oi_degisimi", "op": ">", "value": 2},
            ...
          ]
        }
        """
        for rule in self.red_set_rules:
            if self._evaluate_rule(rule, features):
                self.logger.info(
                    f"Kirmizi set eslesti: {rule['name']} "
                    f"(hit_rate {rule.get('hit_rate', 0):.0%})"
                )
                return rule
        return None

    @staticmethod
    def _evaluate_rule(rule: dict, features: dict[str, Any]) -> bool:
        """Tum kosullar ayni anda saglandiysa True."""
        conditions = rule.get("conditions", [])
        if not conditions:
            return False
        for cond in conditions:
            ftr = cond["feature"]
            op = cond["op"]
            val = cond["value"]
            actual = features.get(ftr)
            if actual is None:
                return False
            try:
                if op == ">" and not (actual > val):
                    return False
                elif op == ">=" and not (actual >= val):
                    return False
                elif op == "<" and not (actual < val):
                    return False
                elif op == "<=" and not (actual <= val):
                    return False
                elif op == "==" and not (actual == val):
                    return False
                elif op == "!=" and not (actual != val):
                    return False
            except (TypeError, ValueError):
                return False
        return True
