"""
utils.py
--------
Yardimci fonksiyonlar: logger setup, retry decorator, zaman/seans hesabi,
veri tipi donusturucu, atomic file write.
"""
from __future__ import annotations

import functools
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TypeVar

from src import config


T = TypeVar("T")


# ---------- LOGGER ----------
def setup_logger(name: str, log_file: Path | None = None) -> logging.Logger:
    """
    Standart logger olusturur. Hem konsola hem dosyaya yazar.
    Aynı isimle tekrar cagrilirsa mevcut handler'lari ekler degil, yeniler.
    """
    logger = logging.getLogger(name)

    # Tekrar setup'ta duplicate handler olmasin
    if logger.handlers:
        return logger

    logger.setLevel(config.LOG_LEVEL)
    fmt = logging.Formatter(config.LOG_FORMAT, config.LOG_DATEFMT)

    # Konsol
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    # Dosya (varsa)
    if log_file is None:
        log_file = config.LOGS_DIR / f"{name}.log"
    try:
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except OSError:
        pass  # CI'da dosya yazilamiyorsa sessizce gec

    logger.propagate = False
    return logger


# ---------- RETRY DECORATOR ----------
def retry(
    max_attempts: int = config.HTTP_MAX_RETRIES,
    backoff_sec: float = config.HTTP_RETRY_BACKOFF_SEC,
    exceptions: tuple = (Exception,),
    logger: logging.Logger | None = None,
) -> Callable:
    """
    Exponential backoff'lu retry decorator. Network/API hatalarinda kullanilir.
    Son denemede de hata varsa exception fırlatır.
    """
    def deco(fn: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs) -> T:
            last_exc: Exception | None = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return fn(*args, **kwargs)
                except exceptions as e:
                    last_exc = e
                    if attempt < max_attempts:
                        wait = backoff_sec * (2 ** (attempt - 1))
                        if logger:
                            logger.warning(
                                f"{fn.__name__} attempt {attempt}/{max_attempts} "
                                f"failed: {type(e).__name__}: {e}. "
                                f"Retrying in {wait:.1f}s..."
                            )
                        time.sleep(wait)
                    else:
                        if logger:
                            logger.error(
                                f"{fn.__name__} attempt {attempt}/{max_attempts} "
                                f"failed: {type(e).__name__}: {e}. Giving up."
                            )
            assert last_exc is not None
            raise last_exc

        return wrapper
    return deco


# ---------- SEANS HESABI ----------
def market_session(dt: datetime | None = None) -> str:
    """
    UTC saatine gore piyasa seansi:
      00-07 UTC  -> asya
      07-13 UTC  -> asya_avrupa  (cakisma)
      13-16 UTC  -> avrupa_ny    (cakisma)
      16-22 UTC  -> ny
      22-24 UTC  -> sessiz
    """
    if dt is None:
        dt = datetime.now(timezone.utc)
    h = dt.hour
    if 0 <= h < 7:
        return "asya"
    elif 7 <= h < 13:
        return "asya_avrupa"
    elif 13 <= h < 16:
        return "avrupa_ny"
    elif 16 <= h < 22:
        return "ny"
    else:
        return "sessiz"


def now_utc() -> datetime:
    """UTC anlik zaman."""
    return datetime.now(timezone.utc)


def now_utc_iso() -> str:
    """ISO 8601 string formatinda UTC zaman (saniye hassasiyetinde)."""
    return now_utc().strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------- SAYISAL YARDIMCILAR ----------
def safe_pct_change(curr: float, prev: float) -> float:
    """Bolum sifir guvenli yuzde degisim. prev=0 ise 0 doner."""
    if prev == 0 or prev is None:
        return 0.0
    return (curr - prev) / prev * 100.0


def safe_ratio(num: float, den: float, default: float = 0.0) -> float:
    """Bolum sifir guvenli oran."""
    if den == 0 or den is None:
        return default
    return num / den


def clamp(x: float, lo: float, hi: float) -> float:
    """Bir degeri [lo, hi] araligina sabitler."""
    return max(lo, min(hi, x))


# ---------- ATOMIC FILE I/O ----------
def atomic_write_json(path: Path, data: Any) -> None:
    """
    JSON'u once .tmp uzantisina yaz, sonra rename. Yarida kalmis dosya olmaz.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def safe_read_json(path: Path, default: Any = None) -> Any:
    """Dosya yoksa veya bozuksa default doner."""
    if not path.exists():
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


# ---------- COIN SEMBOL DONUSTURUCU ----------
def to_binance_symbol(ccxt_symbol: str) -> str:
    """
    ccxt 'BTC/USDT' -> Binance 'BTCUSDT' (REST query param icin).
    """
    return ccxt_symbol.replace("/", "").upper()


def to_sheet_name(ccxt_symbol: str) -> str:
    """
    ccxt 'BTC/USDT' -> Excel sheet 'BTC_verileri'.
    """
    base = ccxt_symbol.split("/")[0].upper()
    return f"{base}_verileri"
