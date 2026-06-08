"""
collector.py  (Bybit versiyonu)
--------------------------------
Binance 451 IP blogu nedeniyle Bybit kullanilir.
Bybit cloud IP'lerini bloke etmez ve ayni verileri sunar.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any
import ccxt
import requests
from src import config
from src.utils import retry, setup_logger, to_binance_symbol

logger = setup_logger("collector")

BYBIT_BASE = "https://api.bybit.com"

@dataclass
class SymbolBundle:
    symbol: str
    ohlcv: dict[str, list[list[float]]] = field(default_factory=dict)
    ticker: dict[str, Any] = field(default_factory=dict)
    order_book: dict[str, Any] = field(default_factory=dict)
    trades: list[dict[str, Any]] = field(default_factory=list)
    funding_rate: dict[str, Any] = field(default_factory=dict)
    open_interest: dict[str, Any] = field(default_factory=dict)
    oi_history: list[dict[str, Any]] = field(default_factory=list)
    funding_history: list[dict[str, Any]] = field(default_factory=list)
    top_lspos_ratio: list[dict[str, Any]] = field(default_factory=list)
    top_lsacc_ratio: list[dict[str, Any]] = field(default_factory=list)
    global_ls_ratio: list[dict[str, Any]] = field(default_factory=list)
    taker_ratio_5m: list[dict[str, Any]] = field(default_factory=list)
    taker_ratio_15m: list[dict[str, Any]] = field(default_factory=list)
    taker_ratio_1h: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

@dataclass
class MacroBundle:
    fng_current: float = 0.0
    fng_7d_avg: float = 0.0
    fng_history: list[dict[str, Any]] = field(default_factory=list)
    btc_dominance: float = 0.0
    usdt_dominance: float = 0.0
    total_mc_change_24h: float = 0.0
    errors: list[str] = field(default_factory=list)

class DataCollector:
    def __init__(self) -> None:
        self.logger = logger
        self.spot = ccxt.bybit({
            "enableRateLimit": True,
            "timeout": config.HTTP_TIMEOUT_SEC * 1000,
        })
        self.futures = ccxt.bybit({
            "enableRateLimit": True,
            "timeout": config.HTTP_TIMEOUT_SEC * 1000,
        })
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "trade-system/1.0"})

    def _futures_sym(self, symbol: str) -> str:
        """BTC/USDT -> BTC/USDT:USDT (Bybit linear perpetual format)"""
        if ":" in symbol:
            return symbol
        base, quote = symbol.split("/")
        return f"{base}/{quote}:{quote}"

    @retry(logger=logger, exceptions=(ccxt.NetworkError, ccxt.ExchangeError))
    def fetch_ticker(self, symbol: str) -> dict[str, Any]:
        return self.spot.fetch_ticker(symbol)

    @retry(logger=logger, exceptions=(ccxt.NetworkError, ccxt.ExchangeError))
    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int) -> list[list[float]]:
        return self.spot.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)

    @retry(logger=logger, exceptions=(ccxt.NetworkError, ccxt.ExchangeError))
    def fetch_order_book(self, symbol: str, limit: int = config.ORDER_BOOK_LIMIT) -> dict[str, Any]:
        return self.spot.fetch_order_book(symbol, limit=limit)

    @retry(logger=logger, exceptions=(ccxt.NetworkError, ccxt.ExchangeError))
    def fetch_trades(self, symbol: str, limit: int = config.TRADES_LIMIT) -> list[dict[str, Any]]:
        return self.spot.fetch_trades(symbol, limit=limit)

    @retry(logger=logger, exceptions=(ccxt.NetworkError, ccxt.ExchangeError))
    def fetch_funding_rate(self, symbol: str) -> dict[str, Any]:
        return self.futures.fetch_funding_rate(self._futures_sym(symbol))

    @retry(logger=logger, exceptions=(ccxt.NetworkError, ccxt.ExchangeError))
    def fetch_open_interest(self, symbol: str) -> dict[str, Any]:
        return self.futures.fetch_open_interest(self._futures_sym(symbol))

    @retry(logger=logger, exceptions=(ccxt.NetworkError, ccxt.ExchangeError))
    def fetch_funding_history(self, symbol: str, limit: int = 21) -> list[dict[str, Any]]:
        return self.futures.fetch_funding_rate_history(
            self._futures_sym(symbol), limit=limit
        )

    @retry(logger=logger, exceptions=(requests.RequestException, ValueError))
    def _bybit_get(self, endpoint: str, params: dict) -> dict:
        r = self.session.get(
            f"{BYBIT_BASE}{endpoint}",
            params=params,
            timeout=config.HTTP_TIMEOUT_SEC,
        )
        r.raise_for_status()
        data = r.json()
        if data.get("retCode", -1) != 0:
            raise ValueError(f"Bybit API hata: {data.get('retMsg')}")
        return data.get("result", {})

    def fetch_ls_ratio(self, symbol: str, period: str = "5min") -> list[dict[str, Any]]:
        try:
            sym = to_binance_symbol(symbol)
            result = self._bybit_get(
                "/v5/market/account-ratio",
                {"category": "linear", "symbol": sym, "period": period, "limit": 1},
            )
            items = result.get("list", [])
            out = []
            for item in items:
                try:
                    buy = float(item.get("buyRatio", 0.5))
                    sell = float(item.get("sellRatio", 0.5))
                    ratio = buy / sell if sell else 1.0
                    out.append({"longShortRatio": ratio, "buySellRatio": ratio})
                except Exception:
                    pass
            return out
        except Exception as e:
            self.logger.debug(f"Bybit L/S ratio hata {symbol}: {e}")
            return []

    def fetch_oi_history(self, symbol: str, period: str = "5m", limit: int = 12) -> list[dict[str, Any]]:
        try:
            sym = to_binance_symbol(symbol)
            period_map = {"5m": "5min", "15m": "15min", "30m": "30min",
                          "1h": "1h", "4h": "4h", "1d": "1d"}
            bybit_period = period_map.get(period, "5min")
            result = self._bybit_get(
                "/v5/market/open-interest",
                {"category": "linear", "symbol": sym,
                 "intervalTime": bybit_period, "limit": limit},
            )
            return result.get("list", [])
        except Exception as e:
            self.logger.debug(f"Bybit OI history hata {symbol}: {e}")
            return []

    def fetch_top_trader_lspos_ratio(self, symbol: str, period: str = "5m", limit: int = 1) -> list[dict[str, Any]]:
        return self.fetch_ls_ratio(symbol, "5min")

    def fetch_top_trader_lsacc_ratio(self, symbol: str, period: str = "5m", limit: int = 1) -> list[dict[str, Any]]:
        return self.fetch_ls_ratio(symbol, "5min")

    def fetch_global_ls_ratio(self, symbol: str, period: str = "5m", limit: int = 1) -> list[dict[str, Any]]:
        return self.fetch_ls_ratio(symbol, "5min")

    def fetch_taker_ratio(self, symbol: str, period: str = "5m", limit: int = 1) -> list[dict[str, Any]]:
        return self.fetch_ls_ratio(symbol, "5min")

    @retry(logger=logger, exceptions=(requests.RequestException, ValueError))
    def fetch_fng(self, limit: int = 7) -> list[dict[str, Any]]:
        url = config.ALTERNATIVE_ME_FNG.format(limit=limit)
        r = self.session.get(url, timeout=config.HTTP_TIMEOUT_SEC)
        r.raise_for_status()
        return r.json().get("data", [])

    @retry(logger=logger, exceptions=(requests.RequestException, ValueError))
    def fetch_global_market(self) -> dict[str, Any]:
        r = self.session.get(config.COINGECKO_GLOBAL, timeout=config.HTTP_TIMEOUT_SEC)
        r.raise_for_status()
        return r.json().get("data", {})

    def collect_for_symbol(self, symbol: str) -> SymbolBundle:
        bundle = SymbolBundle(symbol=symbol)
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
                bundle.errors.append(f"ohlcv_{tf}: {e}")
        try:
            bundle.ticker = self.fetch_ticker(symbol)
        except Exception as e:
            bundle.errors.append(f"ticker: {e}")
        try:
            bundle.order_book = self.fetch_order_book(symbol)
        except Exception as e:
            bundle.errors.append(f"order_book: {e}")
        try:
            bundle.trades = self.fetch_trades(symbol)
        except Exception as e:
            bundle.errors.append(f"trades: {e}")
        try:
            bundle.funding_rate = self.fetch_funding_rate(symbol)
        except Exception as e:
            bundle.errors.append(f"funding_rate: {e}")
        try:
            bundle.funding_history = self.fetch_funding_history(symbol, 21)
        except Exception as e:
            bundle.errors.append(f"funding_history: {e}")
        try:
            bundle.open_interest = self.fetch_open_interest(symbol)
        except Exception as e:
            bundle.errors.append(f"open_interest: {e}")
        try:
            bundle.oi_history = self.fetch_oi_history(symbol)
        except Exception as e:
            bundle.errors.append(f"oi_history: {e}")
        try:
            ls = self.fetch_ls_ratio(symbol)
            bundle.top_lspos_ratio = ls
            bundle.top_lsacc_ratio = ls
            bundle.global_ls_ratio = ls
            bundle.taker_ratio_5m = ls
            bundle.taker_ratio_15m = ls
            bundle.taker_ratio_1h = ls
        except Exception as e:
            bundle.errors.append(f"ls_ratio: {e}")
        if bundle.errors:
            self.logger.warning(f"{symbol}: {len(bundle.errors)} hata")
        else:
            self.logger.info(f"{symbol}: tum veriler OK")
        return bundle

    def collect_btc_reference(self) -> SymbolBundle:
        self.logger.info("BTC referans verileri cekiliyor...")
        return self.collect_for_symbol("BTC/USDT")

    def collect_eth_reference(self) -> SymbolBundle | None:
        try:
            return self.collect_for_symbol("ETH/USDT")
        except Exception as e:
            self.logger.error(f"ETH ref hata: {e}")
            return None

    def collect_macro(self) -> MacroBundle:
        macro = MacroBundle()
        try:
            fng_data = self.fetch_fng(limit=7)
            if fng_data:
                macro.fng_current = float(fng_data[0]["value"])
                vals = [float(d["value"]) for d in fng_data]
                macro.fng_7d_avg = sum(vals) / len(vals)
                macro.fng_history = fng_data
        except Exception as e:
            macro.errors.append(f"fng: {e}")
        try:
            gd = self.fetch_global_market()
            mp = gd.get("market_cap_percentage", {})
            macro.btc_dominance = float(mp.get("btc", 0.0))
            macro.usdt_dominance = float(mp.get("usdt", 0.0))
            macro.total_mc_change_24h = float(
                gd.get("market_cap_change_percentage_24h_usd", 0.0)
            )
        except Exception as e:
            macro.errors.append(f"global: {e}")
        return macro
