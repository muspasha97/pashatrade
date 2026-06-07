# Trade System v1.0

Otonom kripto kantitatif paper-trading & pattern detection altyapisi.
GitHub Actions ile her 15 dakikada bir calisir, 20 coin uzerinde ~50 feature
toplar, ATR tabanli sanal islemler acar, haftada bir basarili pattern'leri
"kirmizi set" olarak isaretler.

## ICERIK

- 20 coin, 7 ayri Excel dosyasinda (gruplar bazinda)
- Multi-timeframe teknik analiz (5m, 15m, 1h, 4h, 1d)
- Order book microstructure (imbalance, duvar, spoofing)
- Futures sentiment (top trader L/S, taker oranlari, funding)
- ATR tabanli dinamik TP/SL (R:R = 1:2)
- Haftalik otomatik pattern detection -> `basarili_kurallar.json`
- Tamamen serverless (GitHub Actions ucretsiz katmaninda calisir)

---

## ADIM ADIM KURULUM

### 1. GitHub'da yeni REPO ac

1. GitHub'a giris yap
2. Sag ust `+` -> `New repository`
3. **Repository name:** `trade-system` (veya istedigin)
4. **Visibility:** Private secersen daha guvenli (Public da olur)
5. **Initialize:** README ekleme, lisans ekleme (biz kendi dosyalarimizi
   pushlayacagiz) - HEPSI BOS BIRAK
6. `Create repository`

### 2. Lokalde repoyu klonla

```bash
# Bash veya PowerShell
git clone https://github.com/<KULLANICI_ADIN>/trade-system.git
cd trade-system
```

### 3. Bu zip'in icerigini kopyala

`trade_system_full.zip` dosyasini ac, icindeki tum dosya ve klasorleri
`trade-system/` klasorune kopyala. Sonuc su yapida olmali:

```
trade-system/
├── .github/
│   └── workflows/
│       ├── scalp_run.yml
│       └── optimizer_run.yml
├── data/                  # bos klasor (Excel'ler burada olusacak)
├── logs/                  # bos klasor
├── rules/                 # bos klasor
├── src/
│   ├── __init__.py
│   ├── collector.py
│   ├── config.py
│   ├── decision.py
│   ├── excel_io.py
│   ├── indicators.py
│   ├── main.py
│   ├── microstructure.py
│   ├── optimizer.py
│   ├── state.py
│   ├── trader.py
│   └── utils.py
├── state/                 # bos klasor
├── requirements.txt
├── README.md
└── .gitignore
```

### 4. .gitignore ekle (zaten zip icinde)

`.gitignore` icerigi:
```
__pycache__/
*.pyc
*.log
*.tmp
*.lock
.DS_Store
.idea/
.vscode/
```

### 5. Lokal olarak TEST et (opsiyonel ama tavsiye)

Once Python 3.10+ kurulu mu kontrol et:
```bash
python --version  # >= 3.10
```

Virtual env olustur ve bagimliliklari kur:
```bash
python -m venv .venv
# Linux/Mac:
source .venv/bin/activate
# Windows:
.venv\Scripts\activate

pip install -r requirements.txt
```

Tek bir donguyu manuel calistir:
```bash
python -m src.main
```

Cikti: `data/trade_majors.xlsx`, `data/trade_layer1.xlsx`, ... dosyalari
olusur. Logs altinda `main.log` var.

### 6. GitHub'a push et

```bash
git add .
git commit -m "initial: trade system v1.0"
git branch -M main
git push -u origin main
```

### 7. GitHub Actions'i ETKINLESTIR

1. GitHub'da reponun sayfasina git
2. Ust menude `Actions` sekmesine tikla
3. "Workflows aren't being run on this forked repository" gibi bir uyari
   gorursen "I understand my workflows, go ahead and enable them" tikla
4. Sol panelde **Trade System - 15dk Cycle** workflow'unu sec
5. Sag ustte `Run workflow` butonuna tikla -> `Run workflow` (manuel test)
6. ~2 dakika sonra calistigini gormeli, yeni bir commit (auto: cycle ...)
   gelir

### 8. Cron OTOMATIK calismaya basliyor

Manuel run basarili olduktan sonra, GitHub her 15 dakikada bir
**scalp_run.yml** workflow'unu otomatik tetikler. Sen hicbir sey yapmana
gerek yok.

> NOT: GitHub free tier'da public repolar icin GitHub Actions ucretsiz
> ve sinirsiz. Private repolar icin aylik 2000 dakika hak var; bizim her
> dongu ~1.5 dakika alir, 24*4*1.5 = 144 dakika/gun = ~4320 dakika/ay.
> Free tier'i AŞAR private repo icin. Cozum: repoyu PUBLIC yap (veri zaten
> public API'lardan, gizlilik yok), veya cron'u 30dk'ya cikar.

### 9. Ilk birkac gun

- Ilk 1-2 saat hicbir islem acilmayabilir (skor esiklerine girmiyor olabilir)
- Her dongude bir satir eklenir (sinyal yoksa bile - veri toplama amacli)
- 2-3 gun sonra `data/` altindaki Excel'leri indir, satirlari gozden gecir
- "izleniyor" sutunu cogunluksa esiklerini gevsetebilirsin (config.py'de
  `DECISION_THRESHOLD_LONG=0.55` -> `0.40` gibi)

### 10. 2-3 hafta sonra: Optimizer

En az 30 KAPANMIS islem (basarili veya basarisiz) her coinde birikince:

- Otomatik: her pazar 03:00 UTC'de `optimizer_run.yml` calisir, kirmizi
  setleri `rules/basarili_kurallar.json`'a yazar
- Manuel: GitHub Actions -> Trade System Haftalik Optimizer ->
  Run workflow

Sonraki dongulerde main.py bu JSON'i yukler, eslesmeleri kirmizi olarak
isaretler.

---

## DOSYA ROLLERI

| Dosya | Sorumluluk |
|-------|------------|
| `src/config.py` | Tum konstantlar - coin listesi, esikler, endpointler |
| `src/utils.py` | Logger, retry, seans hesabi, atomik JSON |
| `src/collector.py` | Tum API cagrilari (Binance spot/futures, F&G, CoinGecko) |
| `src/state.py` | Onceki dongu snapshot'lari, spoofing, likidasyon proxy |
| `src/indicators.py` | RSI, MACD, ATR, BB, VWAP, POC, hacim profili |
| `src/microstructure.py` | Order book imbalance, duvar, CVD, BTC korelasyon |
| `src/decision.py` | Tahmin uretici (heuristic + red set) |
| `src/trader.py` | Yeni islem ac, pending TP/SL kontrol |
| `src/excel_io.py` | Multi-sheet atomik Excel yazma |
| `src/main.py` | Orchestrator - 15dk cron entry point |
| `src/optimizer.py` | Haftalik pattern detection |

---

## AYAR DEGISTIRME

`src/config.py` icinde:

```python
# Coin eklemek/cikarmak
COIN_GROUPS = {
    "majors": ["BTC/USDT", "ETH/USDT"],
    # ...yeni grup ekleyebilirsin
}

# Risk parametreleri
ATR_TARGET_MULT = 2.0   # TP carpani
ATR_STOP_MULT = 1.0     # SL carpani  (R:R = TP/SL)
RISK_PER_TRADE_USD = 100.0

# Karar esikleri
DECISION_THRESHOLD_LONG  = 0.55
DECISION_THRESHOLD_SHORT = -0.55

# Likidasyon proxy
LIQ_PROXY_OI_DROP_PCT = 2.0
LIQ_PROXY_PRICE_MOVE_PCT = 0.5
```

---

## EXCEL FORMATI

Her grup Excel'inde, her coin icin ayri sheet (`BTC_verileri`, `ETH_verileri` ...).
Sutun semasi (`excel_io.py` `COLUMN_SCHEMA` listesi):

**Ana 21 sutun (orijinal sartname):**
tarih, seans, btc_trendi, korku_endeksi, tahta_al_sat_orani, cvd_miktari,
oi_degisimi, funding_oran, duvar_mesafesi, iptal_edilen_duvar_orani,
btc_korelasyonu, poc_uzakligi, likidasyon_miktari, vwap_sapmasi, rsi_degeri,
tahmin_yonu, giris_fiyati, hedef_fiyat, stop_fiyati, islem_durumu,
kirmizi_set_etiketi

**Ek meta (7):** skor, confidence, atr_kullanildi, pozisyon_buyukluk_usd,
cikis_fiyati, kapanis_tarihi, pnl_pct

**Ek feature detaylari (~25):** multi-TF RSI, MACD hist, BB, ATR%, multi-derinlik
imbalance, duvar boyutlari, taker oranlari, top trader L/S, VWAP, POC/VAH/VAL,
BTC dominance, realized vol, ...

---

## SORUN GIDERME

**Problem:** Workflow basarisiz, "ModuleNotFoundError"
**Cozum:** `requirements.txt` repoda var mi kontrol. Yoksa ekle.

**Problem:** "Rate limit exceeded"
**Cozum:** `config.py`'de `HTTP_RETRY_BACKOFF_SEC = 5.0` yap. Veya coin
listesini kucult.

**Problem:** Excel dosyasi corrupt
**Cozum:** `data/` altindaki ilgili dosyayi sil, sistem yeniden olusturur.
Eski veriyi kaybedersin.

**Problem:** Hicbir islem acilmiyor (sadece "izleniyor")
**Cozum:** `DECISION_THRESHOLD_LONG`'u 0.55'ten 0.40'a indir. Veya
heuristic agirliklari `decision.py`'de gozden gecir.

**Problem:** "SSL: CERTIFICATE_VERIFY_FAILED" yerel makinede
**Cozum:** Sertifika store guncellemesi gerek. `pip install --upgrade certifi`
ardindan `pip install -U urllib3 requests` deneyebilirsin.

---

## YOL HARITASI

- [x] Faz 0: Veri kataloglama
- [x] Faz 1-3: Tum cekirdek modullerin yazilmasi
- [ ] Faz 4: 2-3 hafta veri biriktirme (otomatik, mudahale gerekmiyor)
- [ ] Faz 5: Optimizer ile kirmizi set tespiti
- [ ] Faz 6: Insan onayli kirmizi set secimi
- [ ] Faz 7: Telegram entegrasyonu

---

## NOTLAR

- WebSocket likidasyon listener'i bu surumde YOK. Yerine OI dropu + fiyat
  hareketi tabanli **likidasyon proxy** kullaniliyor. Gercek likidasyon
  ihtiyacinda sonradan VPS + WebSocket eklenebilir.
- Coinglass paralidir, kullanilmiyor. Tum veriler Binance native ve
  alternative.me/CoinGecko ucretsiz endpoint'lerinden.
- Sistem stateless - tum durum repo'nun kendisinde (Excel + JSON).
- Hicbir API key gerekmiyor. Tum cagrilar public.
