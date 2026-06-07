"""
collector.py
------------
Tum dis kaynaklardan (Binance spot/futures, alternative.me, CoinGecko) veri
ceken ana sinif. Tek bir DataCollector instance bir donguyu surdurur.

Kullanim:
    collector = DataCollector()
    bundle = collector.collect_for_symbol("BTC/USDT")
    btc_ref = collector.collect_btc_reference()  # tum alt'lar icin BTC bilgisi
    macro = collector.collect_macro()  # F&G, BTC dominance
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import ccxt
import requests

from src import config
from src.utils import (
    retry,
    setup_logger,
    to_binance_symbol,
)


logger = setup_logger("collector")


# ---------- VERI YAPILARI ----------
@dataclass
class SymbolBundle:
    """
    Bir coin icin tek donguden toplanan tum ham veriyi tutar.
    Hesaplama modulleri bu bundle'i alip feature uretir.
    """
    symbol: str
    # OHLCV (kline) - timeframe -> list of [ts, open, high, low, close, volume]
    ohlcv: dict[str, list[list[float]]] = field(default_factory=dict)
    # Spot
    ticker: dict[str, Any] = field(default_factory=dict)
    order_book: dict[str, Any] = field(default_factory=dict)
    trades: list[dict[str, Any]] = field(default_factory=list)
    # Futures
    funding_rate: dict[str, Any] = field(default_factory=dict)
    open_interest: dict[str, Any] = field(default_factory=dict)
    oi_history: list[dict[str, Any]] = field(default_factory=list)
    funding_history: list[dict[str, Any]] = field(default_factory=list)
    # Sentiment (futures data endpoints)
    top_lspos_ratio: list[dict[str, Any]] = field(default_factory=list)
    top_lsacc_ratio: list[dict[str, Any]] = field(default_factory=list)
    global_ls_ratio: list[dict[str, Any]] = field(default_factory=list)
    taker_ratio_5m: list[dict[str, Any]] = field(default_factory=list)
    taker_ratio_15m: list[dict[str, Any]] = field(default_factory=list)
    taker_ratio_1h: list[dict[str, Any]] = field(default_factory=list)
    # Hatalar
    errors: list[str] = field(default_factory=list)


@dataclass
class MacroBundle:
    """Coin-bagimsiz makro veri (F&G, BTC dominance, total MC)."""
    fng_current: float = 0.0
    fng_7d_avg: float = 0.0
    fng_history: list[dict[str, Any]] = field(default_factory=list)
    btc_dominance: float = 0.0
    usdt_dominance: float = 0.0
    total_mc_change_24h: float = 0.0
    errors: list[str] = field(default_factory=list)


# ---------- ANA KOLEKTOR ----------
class DataCollector:
    """
    Tek bir donguyu tamamlayan veri toplayicisi.
    Spot ve futures ayri ccxt instance'lari ile, futures-data endpoint'leri
    icin de requests.Session kullanir.
    """

    def __init__(self) -> None:
        self.logger = logger
        # ccxt - rate limit otomatik
        self.spot = ccxt.binance({
            "enableRateLimit": True,
            "timeout": config.HTTP_TIMEOUT_SEC * 1000,  # ccxt ms
        })
        self.futures = ccxt.binanceusdm({
            "enableRateLimit": True,
            "timeout": config.HTTP_TIMEOUT_SEC * 1000,
        })
        # Non-ccxt requests icin session (connection reuse)
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "trade-system/1.0"})

    # ============= SPOT =============
    @retry(logger=logger, exceptions=(ccxt.NetworkError, ccxt.ExchangeError))
    def fetch_ticker(self, symbol: str) -> dict[str, Any]:
        return self.spot.fetch_ticker(symbol)

    @retry(logger=logger, exceptions=(ccxt.NetworkError, ccxt.ExchangeError))
    def fetch_ohlcv(
        self, symbol: str, timeframe: str, limit: int
    ) -> list[list[float]]:
        return self.spot.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)

    @retry(logger=logger, exceptions=(ccxt.NetworkError, ccxt.ExchangeError))
    def fetch_order_book(
        self, symbol: str, limit: int = config.ORDER_BOOK_LIMIT
    ) -> dict[str, Any]:
        return self.spot.fetch_order_book(symbol, limit=limit)

    @retry(logger=logger, exceptions=(ccxt.NetworkError, ccxt.ExchangeError))
    def fetch_trades(
        self, symbol: str, limit: int = config.TRADES_LIMIT
    ) -> list[dict[str, Any]]:
        return self.spot.fetch_trades(symbol, limit=limit)

    # ============= FUTURES (CCXT) =============
    @retry(logger=logger, exceptions=(ccxt.NetworkError, ccxt.ExchangeError))
    def fetch_funding_rate(self, symbol: str) -> dict[str, Any]:
        return self.futures.fetch_funding_rate(symbol)

    @retry(logger=logger, exceptions=(ccxt.NetworkError, ccxt.ExchangeError))
    def fetch_open_interest(self, symbol: str) -> dict[str, Any]:
        return self.futures.fetch_open_interest(symbol)

    @retry(logger=logger, exceptions=(ccxt.NetworkError, ccxt.ExchangeError))
    def fetch_funding_history(
        self, symbol: str, limit: int = 21
    ) -> list[dict[str, Any]]:
        return self.futures.fetch_funding_rate_history(symbol, limit=limit)

    # ============= FUTURES DATA (HAM REST) =============
    # Bunlar ccxt'de YOK. Manuel requests ile cekiyoruz.
    @retry(
        logger=logger,
        exceptions=(requests.RequestException, ValueError),
    )
    def _futures_data_get(self, url: str, params: dict) -> list[dict[str, Any]]:
        """Generic GET /futures/data/* endpoint cagrisi."""
        r = self.session.get(url, params=params, timeout=config.HTTP_TIMEOUT_SEC)
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, list):
            raise ValueError(f"Beklenmedik response tipi: {type(data)}")
        return data

    def fetch_oi_history(
        self, symbol: str, period: str = "5m", limit: int = 12
    ) -> list[dict[str, Any]]:
        """
        Open interest gecmisi (5dk araliklarla son 12 nokta = 1 saat).
        Response: [{symbol, sumOpenInterest, sumOpenInterestValue, timestamp}, ...]
        """
        return self._futures_data_get(
            config.FAPI_OI_HIST,
            params={
                "symbol": to_binance_symbol(symbol),
                "period": period,
                "limit": limit,
            },
        )

    def fetch_top_trader_lspos_ratio(
        self, symbol: str, period: str = "5m", limit: int = 1
    ) -> list[dict[str, Any]]:
        """Top %20 trader pozisyon long/short orani."""
        return self._futures_data_get(
            config.FAPI_TOP_LSPOS_RATIO,
            params={
                "symbol": to_binance_symbol(symbol),
                "period": period,
                "limit": limit,
            },
        )

    def fetch_top_trader_lsacc_ratio(
        self, symbol: str, period: str = "5m", limit: int = 1
    ) -> list[dict[str, Any]]:
        """Top %20 trader HESAP sayisi long/short orani."""
        return self._futures_data_get(
            config.FAPI_TOP_LSACC_RATIO,
            params={
                "symbol": to_binance_symbol(symbol),
                "period": period,
                "limit": limit,
            },
        )

    def fetch_global_ls_ratio(
        self, symbol: str, period: str = "5m", limit: int = 1
    ) -> list[dict[str, Any]]:
        """Tum traderlarin (perakende dahil) long/short hesap orani."""
        return self._futures_data_get(
            config.FAPI_GLOBAL_LS_RATIO,
            params={
                "symbol": to_binance_symbol(symbol),
                "period": period,
                "limit": limit,
            },
        )

    def fetch_taker_ratio(
        self, symbol: str, period: str = "5m", limit: int = 1
    ) -> list[dict[str, Any]]:
        """Taker buy/sell volume orani."""
        return self._futures_data_get(
            config.FAPI_TAKER_RATIO,
            params={
                "symbol": to_binance_symbol(symbol),
                "period": period,
                "limit": limit,
            },
        )

    # ============= SENTIMENT / MAKRO =============
    @retry(
        logger=logger,
        exceptions=(requests.RequestException, ValueError),
    )
    def fetch_fng(self, limit: int = 7) -> list[dict[str, Any]]:
        """
        alternative.me Fear & Greed Index.
        Response: {"data": [{"value": "...", "value_classification": "...", ...}, ...]}
        """
        url = config.ALTERNATIVE_ME_FNG.format(limit=limit)
        r = self.session.get(url, timeout=config.HTTP_TIMEOUT_SEC)
        r.raise_for_status()
        data = r.json()
        return data.get("data", [])

    @retry(
        logger=logger,
        exceptions=(requests.RequestException, ValueError),
    )
    def fetch_global_market(self) -> dict[str, Any]:
        """
        CoinGecko global piyasa: BTC dominance, USDT dominance, toplam MC degisim.
        """
        r = self.session.get(
            config.COINGECKO_GLOBAL, timeout=config.HTTP_TIMEOUT_SEC
        )
        r.raise_for_status()
        return r.json().get("data", {})

    # ============= TOPLU BUNDLE TOPLAYICI =============
    def collect_for_symbol(self, symbol: str) -> SymbolBundle:
        """
        Bir coin icin tum tek-coin verisini tek bir bundle'da toplar.
        Her API call ayri try-except'te - bir tanesi patlasa diger veriler
        kaybolmaz, bundle.errors'a yazilir.
        """
        bundle = SymbolBundle(symbol=symbol)

        # ----- OHLCV multi-TF -----
        tf_limits = {
            "5m": config.OHLCV_LIMIT_5M,
            "15m": config.OHLCV_LIMIT_15M,
            "1h": config.OHLCV_LIMIT_1H,
            "4h": config.OHLCV_LIMIT_4H,
            "1d": config.OHLCV_LIMIT_1D,
        }
        for tf, lim in tf_limits.items():
            try:
                bundle.ohlcv[tf] = self.fetch_ohlcv(symbol, tf, lim)
            except Exception as e:
                msg = f"ohlcv_{tf} hata: {type(e).__name__}: {e}"
                self.logger.error(f"{symbol} {msg}")
                bundle.errors.append(msg)

        # ----- Spot ticker -----
        try:
            bundle.ticker = self.fetch_ticker(symbol)
        except Exception as e:
            bundle.errors.append(f"ticker hata: {e}")

        # ----- Order book -----
        try:
            bundle.order_book = self.fetch_order_book(symbol)
        except Exception as e:
            bundle.errors.append(f"order_book hata: {e}")

        # ----- Trades -----
        try:
            bundle.trades = self.fetch_trades(symbol)
        except Exception as e:
            bundle.errors.append(f"trades hata: {e}")

        # ----- Futures: funding -----
        try:
            bundle.funding_rate = self.fetch_funding_rate(symbol)
        except Exception as e:
            bundle.errors.append(f"funding_rate hata: {e}")

        try:
            bundle.funding_history = self.fetch_funding_history(symbol, limit=21)
        except Exception as e:
            bundle.errors.append(f"funding_history hata: {e}")

        # ----- Futures: OI -----
        try:
            bundle.open_interest = self.fetch_open_interest(symbol)
        except Exception as e:
            bundle.errors.append(f"open_interest hata: {e}")

        try:
            bundle.oi_history = self.fetch_oi_history(symbol, "5m", 12)
        except Exception as e:
            bundle.errors.append(f"oi_history hata: {e}")

        # ----- Futures data: long/short oranlari -----
        try:
            bundle.top_lspos_ratio = self.fetch_top_trader_lspos_ratio(symbol)
        except Exception as e:
            bundle.errors.append(f"top_lspos_ratio hata: {e}")

        try:
            bundle.top_lsacc_ratio = self.fetch_top_trader_lsacc_ratio(symbol)
        except Exception as e:
            bundle.errors.append(f"top_lsacc_ratio hata: {e}")

        try:
            bundle.global_ls_ratio = self.fetch_global_ls_ratio(symbol)
        except Exception as e:
            bundle.errors.append(f"global_ls_ratio hata: {e}")

        # ----- Taker oranlari (3 farkli period) -----
        try:
            bundle.taker_ratio_5m = self.fetch_taker_ratio(symbol, "5m", 1)
        except Exception as e:
            bundle.errors.append(f"taker_ratio_5m hata: {e}")

        try:
            bundle.taker_ratio_15m = self.fetch_taker_ratio(symbol, "15m", 1)
        except Exception as e:
            bundle.errors.append(f"taker_ratio_15m hata: {e}")

        try:
            bundle.taker_ratio_1h = self.fetch_taker_ratio(symbol, "1h", 1)
        except Exception as e:
            bundle.errors.append(f"taker_ratio_1h hata: {e}")

        if bundle.errors:
            self.logger.warning(
                f"{symbol}: {len(bundle.errors)} hata - "
                f"orneklem: {bundle.errors[0]}"
            )
        else:
            self.logger.info(f"{symbol}: tum veriler basarili")

        return bundle

    def collect_btc_reference(self) -> SymbolBundle:
        """
        BTC referans verisi (her altcoin'in cross-asset feature'lari icin gerekli).
        Tum altcoin'ler isleme baslamadan once 1 kez cekilir.
        """
        self.logger.info("BTC referans verileri cekiliyor...")
        return self.collect_for_symbol("BTC/USDT")

    def collect_eth_reference(self) -> SymbolBundle | None:
        """ETH referans (Layer-2 ve DeFi grubunda kullanilir)."""
        try:
            self.logger.info("ETH referans verileri cekiliyor...")
            return self.collect_for_symbol("ETH/USDT")
        except Exception as e:
            self.logger.error(f"ETH ref hata: {e}")
            return None

    def collect_macro(self) -> MacroBundle:
        """
        Coin-bagimsiz makro veri. Saatte 1 kez yenilenir (cache logic
        main.py'da, burada ham fetch).
        """
        macro = MacroBundle()

        try:
            fng_data = self.fetch_fng(limit=7)
            if fng_data:
                # alternative.me en yeniyi ilk doner
                macro.fng_current = float(fng_data[0]["value"])
                values = [float(d["value"]) for d in fng_data]
                macro.fng_7d_avg = sum(values) / len(values)
                macro.fng_history = fng_data
        except Exception as e:
            macro.errors.append(f"fng hata: {e}")
            self.logger.error(f"F&G fetch hata: {e}")

        try:
            global_data = self.fetch_global_market()
            mcap_pct = global_data.get("market_cap_percentage", {})
            macro.btc_dominance = float(mcap_pct.get("btc", 0.0))
            macro.usdt_dominance = float(mcap_pct.get("usdt", 0.0))
            macro.total_mc_change_24h = float(
                global_data.get("market_cap_change_percentage_24h_usd", 0.0)
            )
        except Exception as e:
            macro.errors.append(f"global hata: {e}")
            self.logger.error(f"CoinGecko global hata: {e}")

        return macro
