# MEXC_GOREV_01 — SONUÇ

**Görev No:** MEXC_GOREV_01
**Tarih:** 2026-09-17
**Amaç:** Claude Project Takeover — mevcut MEXC_PAPER_TRADER projesinin
devralınması, sistemin doğrulanması ve kalıcı `CLAUDE.md` proje
talimatlarının oluşturulması. Bu görev yalnızca **audit + doküman
oluşturma** kapsamındadır; trading kodu, DB verisi veya servis durumu
değiştirilmemiştir.

**Başlangıç HEAD:** `17e34068bbcd4d558a21a22dd62c97d90248d2a3`
(auto start paper scanner on service startup, 2026-09-10T03:14:26+00:00)

---

## PROJECT IDENTITY

- ROOT: `/opt/mexc_paper_trader` ✅ doğrulandı
- BRANCH: `main` ✅ doğrulandı
- REMOTE: `git@github.com:alerdogan/mexc_paper_trader.git` ✅ doğrulandı
- `/home/pronist/pro-portal`'a erişilmedi, dokunulmadı.

## CURRENT HEAD BEFORE

`17e34068bbcd4d558a21a22dd62c97d90248d2a3`

## CURRENT HEAD AFTER

`a785e54b614bdbee085d7a01031edcfbef5076b2`
(docs: add project CLAUDE.md constitution and takeover audit result)

## ARCHITECTURE

Tek dosya FastAPI uygulaması (`app.py`, ~2525 satır) + tek sayfa Jinja2/JS
dashboard (`templates/index.html`) + SQLite (`trader.db`, WAL modu).

- **Scanner**: `engine()` → `scan_once()` — Top-30 hacim evreni + MEXC Spot
  USDT crypto whitelist üzerinde 15m/1h/4h analiz, skor hesaplama.
- **Trading/PAPER engine**: `process_paper_signal()` → `paper_open()` —
  sinyal üretimi, V1 filtre kontrolü, pozisyon açma.
- **Risk engine**: `paper_open()` içinde sıralı kontroller — entry_paused,
  günlük zarar limiti, PAPER bakiye, toplam açık risk, margin yeterliliği.
- **Position management**: `_manage_once()` / `position_engine()` — TP1,
  TP2, stop, break-even, runner trailing yönetimi; `_current_position_transition()`.
- **Worker/supervisor**: `supervise_task()` — `scanner`, `position_engine`,
  `ghost_analyzer` task'ları, hata durumunda `SUPERVISOR_BACKOFF=(1,2,5,10,30)`
  sn ile otomatik yeniden başlama. `start_background_tasks()` zaten çalışan
  task'ı tekrar başlatmaz (duplicate koruması).
- **Startup/recovery**: `@app.on_event('startup')` → `init_db()` (idempotent
  ALTER TABLE'lar) → `PAPER_AUTO_START=True` ise state.running=True →
  task'lar başlar. Açık pozisyonlar DB'de kalıcı olduğu için restart-safe
  (ayrıca doğrulandı — bkz. RESTART/RECOVERY).
- **Dashboard/API**: `/api/health`, `/api/status`, `/api/history`,
  `/api/entry-filter-rejections`, `/api/entry-timing-lab`,
  `/api/strategy-lab`, `/api/stop-analysis`, `/api/live-fee-test/*`,
  pozisyon kapatma uçları.
- **systemd**: `mexc-trader.service`, enabled+active, `User=pronist`,
  `ExecStart=.venv/bin/python -m uvicorn app:app --host 0.0.0.0 --port 8071`,
  `Restart=always`, `EnvironmentFile=-/etc/mexc-trader/secrets.env`.

Detaylar `CLAUDE.md` → "MİMARİ ÖZET" bölümünde kalıcı olarak belgelendi.

## CURRENT STRATEGY

DB `config` tablosundaki fiilen aktif değerler (kod içi `DEFAULTS`'tan
farklı, gerçek çalışan konfigürasyon):

- leverage: **3**
- risk_per_trade_usd: **$20**
- max_alt_notional_usd: **$2000**
- max_btc_notional_usd: **$10000**
- max_total_open_risk_usd: **$500**
- daily_loss_limit_usd: **$500**
- min_free_balance_pct: **%20**
- signal_threshold: **80**
- stop_atr_mult: **1.5 ATR**
- tp1_r / tp1_pct: **1.0R / %30**
- tp2_r / tp2_pct: **2.0R / %30**
- runner_pct: **%40**
- move_be_at_r: **1.0R**
- paper_fee_rate: **%0.08 (taker)**
- paper_balance (başlangıç): **$4000**
- scan_seconds: **30**
- timeframe'ler: **15m (sinyal), 1h, 4h (trend teyidi)**
- LONG/SHORT: her ikisi de destekleniyor; V1 filtresi yalnız LONG'u etkiliyor.

Position lifecycle doğrulandı: `paper_open` → TP1 (%30 kapanış + stop→BE) →
TP2 (%30 kapanış) → runner (%40, opsiyonel `TRAILING_RUNNER` shadow modeli
`max_favorable_R - 1R`) → kapanış (`close_reason`: `STOP`/`MANUAL`/
`EMERGENCY_CLOSE`/`RUNNER_TRAILING_STOP`).

**Bu görevde hiçbir trading parametresi değiştirilmedi.**

## PAPER/LIVE SAFETY

- CURRENT mode: **PAPER** ✅ (`/api/health` → `"mode":"PAPER"`)
- LIVE DISARMED: ✅ — `LIVE_FEE_TEST` tek gerçek emir yolu; DB'de zaten 1
  `COMPLETED` kayıt (id=5, 2026-09-03) var, bu yüzden `arm_live_fee_test()`
  yeniden arm edilmesini reddediyor (`409 Tek seferlik LIVE_FEE_TEST daha
  önce tamamlandı; yeniden kurulamaz.`). Ayrıca yalnız localhost'tan
  (`_require_local`) erişilebilir.
- LIVE_FEE_TEST durumu: **COMPLETED (tek kullanımlık, kapalı)**.
- PAPER reboot auto-start: ✅ `PAPER_AUTO_START=True`, `startup()` içinde
  doğrulandı.
- LIVE auto-start yok: ✅ kodda LIVE'ı otomatik arm/başlatan hiçbir yol
  bulunmadı.
- Bu görevde LIVE işlem oluşturulmadı, LIVE arm edilmedi, LIVE test
  çalıştırılmadı.

## V1

`ENTRY_QUALITY_FILTER_V1` — **ACTIVE**. `process_paper_signal()` içinde
yalnız yeni LONG sinyallerini kapı kontrolünden geçirir (coin RSI≥64,
BTC/ETH 15m volume ratio<0.55, coin LONG volume_score=0 → red). SHORT
etkilenmez. Reddedilenler `entry_filter_rejections` + `reject_shadow_trades`
tablolarına kaydedilir, CURRENT'a etkisi kod okuması ile doğrulandı.

## V2

`ENTRY_QUALITY_FILTER_V2_RESEARCH` — **RESEARCH (shadow)**. `paper_open()`
içinde gerçek pozisyon DB'ye yazıldıktan SONRA `_insert_entry_quality_v2_research()`
çağrılıyor; CURRENT açılış/kapanış kararını etkilemiyor, non-blocking
doğrulandı.

## V3

`ENTRY_QUALITY_FILTER_V3_RESEARCH` — **RESEARCH (shadow)**. Aynı desen,
non-blocking, `paper_open()` sonrası kayıt.

## STRATEGY LAB

Modeller: `CURRENT`, `NO_STOP_MINI`, `SMART_EXIT`, `TRAILING_RUNNER` —
**RESEARCH (shadow)**. `strategy_lab_create()` her yeni PAPER girişinde
paralel sanal deney açıyor; ana bakiye/risk/pozisyon tablolarını
etkilemediği kod okuması ile doğrulandı.

## ENTRY TIMING LAB

`CONFIRMED_5M_ENTRY` (`ENTRY_TIMING_LAB_VERSION='CONFIRMED_5M_ENTRY_V1'`) —
**RESEARCH (shadow)**. `_insert_entry_timing_candidate()` de `paper_open()`
içinde gerçek pozisyon yazıldıktan sonra çağrılıyor; CONFIRMED_5M_ENTRY'nin
**CURRENT işlemleri değiştirmediği doğrulandı** (görev md. 7 gereği özellikle
kontrol edildi). LONG-only davranış, tamamlanmış 5m mum teyidi, 30 dakika
(`ENTRY_TIMING_CONFIRMATION_SECONDS`), chase R (`ENTRY_TIMING_CHASE_R=0.75`)
parametreleri koda göre teyit edildi; ayrı fonksiyonel test/simülasyon bu
audit kapsamına girmedi (kod okuması + mevcut `tests/test_entry_timing_lab.py`
suite'i geçti).

## DATABASE HEALTH

- `PRAGMA integrity_check` → **ok** (read-only bağlantı, `mode=ro` ile).
- Şema: 18 tablo (`config`, `positions`, `logs`, `paper_epochs`,
  `position_score_snapshots`, `market_regime_snapshots`,
  `entry_filter_rejections`, `entry_filter_active_setups`,
  `reject_shadow_trades`, `entry_quality_filter_v2_research(+meta)`,
  `entry_quality_filter_v3_research(+meta)`, `entry_timing_lab_candidates`,
  `entry_timing_lab_meta`, `strategy_lab_experiments`, `strategy_lab_runs`,
  `live_fee_tests`, `sqlite_sequence`).
- Pozisyonlar: 517 CLOSED, 8 OPEN (tümü `mode='PAPER'`, `paper_epoch_id=1`).
- Bu görevde DB'ye hiçbir yazma işlemi yapılmadı (yalnız `mode=ro` read-only
  sorgular).

## SERVICE/SCANNER HEALTH

- `systemctl status mexc-trader`: **active (running)**, enabled, 1 hafta+
  uptime, restart yok.
- `/api/health`: `service_status=healthy`, `mode=PAPER`,
  `mexc_api_connection=BAĞLI`, `scanner.alive=true running=true
  last_success=2026-09-17T18:14:29 last_error=null`,
  `position_engine.alive=true running=true` (geçmişte tek bir transient
  `ConnectError`, sonrasında başarılı çalışıyor), `ghost_analyzer.alive=true
  running=true`, `database_connectivity=true`.
- Son WARN log'ları: 2026-09-17 ~17:15-17:24 arası kısa süreli MEXC
  `ConnectError` — otomatik retry ile kendini toparladı, sonraki scan'ler
  başarılı. Kritik ERROR bulunmadı.
- V1 ACTIVE: ✅. LIVE DISARMED: ✅.
- Bu görevde servis restart/reload/reboot yapılmadı.

## RESTART/RECOVERY

- Son servis restart'ı: 2026-09-10 (mevcut commit ile ilişkili). Sonrasında
  açılan pozisyonlar (örn. 2026-09-15, 2026-09-16, 2026-09-17 tarihli açık
  pozisyonlar) DB'de mevcut ve `position_engine` tarafından yönetilmeye
  devam ediyor — **restart-safe pozisyon kalıcılığı doğrulandı** (ayrı bir
  "recovery" kod yolu yok, state doğrudan DB'den okunuyor).
- Pending research recovery: V2/V3/Entry Timing Lab kayıtları da
  `position_id` üzerinden kalıcı; ayrı bir kurtarma adımı gerektirmiyor.

## SECURITY

- Git tracked dosyalarda secret/credential/db/backup **YOK**
  (`git ls-files` ile doğrulandı; `deploy/mexc-trader-credentials.conf`
  yalnız `EnvironmentFile=` pointer'ı, gerçek değer içermiyor).
- `.gitignore` `trader.db`, `backups/`, `.env*`, `secrets.env*`, `*.key`,
  `*.pem`, `*credentials*` vb. hariç tutuyor.
- `credential_get()` önce env var okuyor, yalnız `darwin` platformda
  Keychain'e düşüyor — Linux production için güvenli, doğrulandı.
- `/etc/mexc-trader/secrets.env` erişimi bu oturumdan **Permission denied**
  (beklenen/istenen durum — dosya içeriğine bakılmadı).
- Bu raporda hiçbir secret değeri gösterilmedi.

## CLAUDE.md CREATED

`/opt/mexc_paper_trader/CLAUDE.md` oluşturuldu. İçerik: PROJECT ISOLATION,
PAPER/LIVE SAFETY, DATABASE SAFETY, RESEARCH INTEGRITY, EXISTING STRATEGY
PROTECTION (+ research sistemleri durum tablosu), SECRETS, TEST/DEPLOY/GIT
WORKFLOW, APPROVAL/USER INTERRUPTION POLICY, RESULT FILE PROTOCOL, ve
referans amaçlı mimari özet. `AGENTS.md` içindeki güvenlik ve onay
kuralları korunmuş, bunlara referans verilmiştir.

## TESTS

- `.venv/bin/python -m py_compile app.py` → **SYNTAX OK**
- `.venv/bin/python -m pytest tests/ -q` → **125 passed, 1 xfailed**,
  2 deprecation warning (FastAPI `on_event`, fonksiyonel etkisi yok).
- `tests/conftest.py` prod `trader.db`'ye bağlanmayı otomatik engelliyor
  (`forbid_production_database` fixture) — testler prod veriye dokunmadı.

## GIT/GITHUB

- `git diff` (CLAUDE.md ve bu sonuç dosyası dışında) → **boş**, beklenmeyen
  değişiklik yok.
- Staged/commit öncesi kontrol: `trader.db`, `.env`, `secrets`,
  `credentials`, `backups/` → **staged değil**.
- Commit: `a785e54b614bdbee085d7a01031edcfbef5076b2` — `CLAUDE.md` ve
  `MEXC_GOREV_01_SONUC.md` eklendi (bu düzeltme öncesi hash; sonuç
  dosyasındaki hash düzeltmesi için ayrı bir takip commit'i atıldı).
- `git push origin main` çalıştırıldı.

## IMPORTANT RISKS

- **En son DB yedeği 2026-09-01 tarihli** (`backups/trader-20260901T*.db`);
  bu tarihten sonra 500+ yeni kapalı pozisyon ve tüm research verisi
  birikmiş. Bir sonraki DB'ye dokunan görevden önce güncel, bütünlüğü
  doğrulanmış bir yedek alınması önerilir.
- Transient MEXC `ConnectError` uyarıları düzenli aralıklarla görülüyor;
  şu an otomatik retry ile toparlanıyor ama izlenmeye devam edilmeli.
- LIVE_FEE_TEST akışı tek kullanımlık olarak "tükenmiş" durumda (COMPLETED);
  gelecekte tekrar bir fee doğrulaması gerekirse bunun DB'de nasıl
  yeniden etkinleştirileceği (yeni bir mekanizma mı, mevcut `COMPLETED`
  kaydının kaldırılması mı) ayrı bir kullanıcı kararı gerektirir — bu
  görevde dokunulmadı.

## OPEN ITEMS

- Güncel DB yedeği alınması (kullanıcı onayı/talebiyle, ayrı görev).
- Research sistemlerinden herhangi birinin (V2/V3/Entry Timing Lab) CURRENT'a
  taşınıp taşınmayacağına dair karar kullanıcıya aittir; bu görev kapsamında
  değerlendirilmedi.

## TAKEOVER RESULT

Proje incelendi, mevcut güvenlik/onay kuralları (`AGENTS.md`) doğrulandı ve
korundu, mimari + CURRENT strateji + research sistemleri kod ve DB üzerinden
teyit edildi, DB bütünlüğü `ok`, servis `healthy`/`PAPER`/scanner çalışıyor,
LIVE disarmed, hiçbir trading parametresi veya production verisi
değiştirilmedi. Kalıcı `CLAUDE.md` oluşturuldu.

**READY FOR NEXT MEXC TASK: YES**
