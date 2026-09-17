# MEXC_GOREV_02 — SONUÇ

**Görev No:** MEXC_GOREV_02
**Tarih:** 2026-09-17
**Amaç:** Mevcut `CONFIRMED_5M_ENTRY_V1` (Entry Timing Lab) yanına, tamamen
research-only, ikinci bir forward-hypothesis timing modeli eklemek:
`MOMENTUM_5M_ENTRY_V1`. Soru: CURRENT'ın LONG açtığı aynı setup'ta, hemen
girmek yerine gerçek 5m momentum continuation/breakout başladıktan sonra
sanal girilseydi sonuç iyileşir miydi? No historical backfill; yalnız
deploy sonrası ileriye dönük veri toplanacak.

**TASK STATUS: SUCCESS**

---

## HEAD BEFORE

`8775c3bcb88ef8358f618a759a80c54ed748ed83`
(docs: record exact commit hash in takeover result file)

## HEAD AFTER

Bu görevin commit'i — bkz. "GIT/GITHUB" bölümü (dosya push'tan hemen önce
güncellenecek şekilde son commit hash'i orada raporlanır).

## PROJECT IDENTITY DOĞRULAMASI

ROOT `/opt/mexc_paper_trader`, BRANCH `main`,
REMOTE `git@github.com:alerdogan/mexc_paper_trader.git` — hepsi doğrulandı.
`/home/pronist/pro-portal`'a dokunulmadı.

## BACKUP

- Path: `/opt/mexc_paper_trader/backups/trader-20260917T183220Z.db`
- Yöntem: SQLite online backup API (`sqlite3.Connection.backup()`),
  WAL-safe, tutarlı snapshot; kaynak read-only (`mode=ro`) açıldı.
- Doğrulama: `PRAGMA integrity_check` → **ok**; `positions` satır sayısı
  525 (kaynakla birebir eşleşti).
- Mevcut `trader.db` ve geçmiş asla silinmedi/resetlenmedi/üzerine
  yazılmadı; migration additive olarak canlı DB üzerinde çalıştırıldı.

## CHANGED FILES

- `app.py` — yeni sabitler, DB şeması, candidate/candle/shadow mantığı,
  scan/position-engine entegrasyonu, `/api/momentum-5m-entry` endpoint'i.
- `templates/index.html` — yeni "12 · MOMENTUM 5M ENTRY" sekmesi (nav,
  sayfa, JS yükleme fonksiyonu, 5 sn refresh döngüsüne kayıt).
- `tests/test_momentum_5m_entry.py` — yeni, 20 test.
- `MEXC_GOREV_02_SONUC.md` — bu dosya.
- Değiştirilmeyenler (bilinçli): CURRENT entry/risk/sizing/stop/TP1/TP2/
  runner/fee, `ENTRY_QUALITY_FILTER_V1` mantığı, V2/V3 research, Strategy
  Lab, `entry_timing_lab_candidates` (CONFIRMED_5M_ENTRY_V1) — hiçbiri
  dokunulmadı; regresyon testleriyle doğrulandı (aşağıya bakın).

## DB MIGRATION / TABLES

Additive, idempotent (`CREATE TABLE IF NOT EXISTS` + `INSERT OR IGNORE`),
mevcut hiçbir tablo drop/recreate edilmedi. 3 yeni tablo:

1. **`momentum_5m_entry_meta`** — `version TEXT PRIMARY KEY, deployed_at,
   params_json`. Deploy anında dondurulmuş parametre snapshot'ı
   (`model_name, max_wait_minutes=30, min_move_R=0.10, max_chase_R=0.75,
   min_volume_ratio=0.80, completed_candles_only=true,
   rsi_non_deterioration=true, breakout_definition=<açık metin>`). Bu V1
   cohort'un parametreleri artık sessizce değiştirilmeyecek; yeni
   parametre gerekirse V2 açılır.
2. **`momentum_5m_entry_candidates`** — `entry_timing_lab_candidates` ile
   aynı desende: `current_position_id UNIQUE` (duplicate guard),
   `original_current_entry_price/atr/5m_rsi` (dondurulmuş orijinal
   referanslar), `breakout_reference_high` + `last_evaluated_candle_ts`
   (restart-safe running state), `confirmation_status` (PENDING →
   CONFIRMED | NO_MOMENTUM_TIMEOUT | MOMENTUM_CHASED_EXPIRED |
   CANCELLED_INVALID; CONFIRMED → CLOSED), shadow entry/stop/qty/TP/MAE/
   MFE/PnL/fee alanları.
3. **`momentum_5m_entry_candle_evaluations`** — sınırlı (candidate başına
   en fazla ~6 satır, 30dk/5dk) audit log: her değerlendirilen tamamlanmış
   mumun OHLC'si, previous_close, breakout_reference, RSI, volume_ratio,
   move_r ve 7 koşulun (bullish/previous_close/breakout/rsi/volume/
   min_move/chase) + `confirmed_this_candle` pass/fail bayrakları.
   `UNIQUE(candidate_id,candle_open_ts)` ile idempotent insert.

`entry_timing_lab_candidates` şeması veya verisi **hiç değiştirilmedi**.

## MOMENTUM_5M_ENTRY_V1 — TAM KURAL SETİ

- **Model/versiyon:** `MOMENTUM_5M_ENTRY` / `MOMENTUM_5M_ENTRY_V1`.
- **Eligible cohort:** Yalnız LONG; yalnız `ENTRY_QUALITY_FILTER_V1`
  PASS + gerçek CURRENT PAPER LONG başarıyla açıldığında (`paper_open()`
  write() transaction'ı içinde, `_insert_entry_timing_candidate` ile aynı
  konum/aynı `position_id`) oluşur — CONFIRMED_5M_ENTRY_V1 ile birebir
  aynı eligible cohort. `INSERT OR IGNORE` + `UNIQUE(current_position_id)`
  ile duplicate/atomicity garantisi.
- **Pencere:** Candidate oluşumundan itibaren maksimum 30 dakika
  (`expires_at`).
- **Yalnız tamamlanmış 5m mumlar** kullanılır
  (`_momentum_5m_completed_candles`); forming/incomplete mum asla
  değerlendirilmez.
- **CONFIRMED şartları (tek bir mumda TÜMÜ birden sağlanmalı):**
  A) `close > open` (bullish)
  B) `close > previous completed 5m close`
  C) **Breakout:** `close > running_breakout_reference_high` — referans,
     candidate oluşumundaki CURRENT LONG entry fiyatında (`original_current_entry_price`)
     çapalanır (bu an itibariyle zaten bilinen, geleceğe ait olmayan bir
     değerdir) ve her değerlendirilen (onaylamayan) mumun `high`'ı ile
     genişler — **ilk değerlendirilebilir mum kendi high'ını kendi
     referansı olarak asla kullanmaz** (test:
     `test_breakout_reference_expands_and_first_candle_uses_no_future_info`).
  D) **RSI non-deterioration:** `candle.rsi >= original_5m_rsi` (baseline
     candidate oluşumunda donuyor; baseline yoksa koşul otomatik geçer —
     CONFIRMED_5M_ENTRY_V1 ile aynı fallback).
  E) `volume_ratio >= 0.80`.
  F) `+0.10R <= move_r < 0.75R` (move, mumun `close`'undan CURRENT'ın
     ORİJİNAL giriş fiyatına göre; R paydası aşağıya bakın).
- **R paydası:** `abs(original_atr * settings['stop_atr_mult'])`
  (taban `original_entry*0.002`), **CURRENT position'ın o anki (BE/+1R
  taşınmış) `stop` alanından değil, candidate oluşumunda dondurulan
  `original_atr`'den** hesaplanır — CURRENT'ın stop'u sonradan BE/TP2'ye
  taşınsa bile bu payda değişmez (test:
  `test_original_r_denominator_unaffected_by_current_stop_move`).
- **Terminal state'ler:** `PENDING`, `CONFIRMED`, `NO_MOMENTUM_TIMEOUT`,
  `MOMENTUM_CHASED_EXPIRED`, `CANCELLED_INVALID` (savunma amaçlı: geçersiz
  risk verisi — `original_atr`/`original_current_entry_price<=0`).
  `MOMENTUM_CHASED_EXPIRED`: confirmation olmadan `move_r>=0.75` (hem
  canlı fiyatla ön-kontrol, hem her mum işlenirken). `NO_MOMENTUM_TIMEOUT`:
  30 dk içinde confirmation yok. Her terminal geçiş
  `WHERE confirmation_status='PENDING'` guard'ı ile korunur → bir
  candidate yalnız BİR terminal state'e geçebilir (test:
  `test_terminal_transition_is_atomic_and_idempotent`).
- **Restart recovery:** Ayrı bir kurtarma kodu yok/gerekmiyor — tüm
  state DB'de (`confirmation_status='PENDING'` satırları), her scan
  (30sn) ve position_engine (15sn throttle) döngüsünde otomatik yeniden
  işlenir (test: `test_restart_recovery_migration_idempotency_and_existing_positions_unaffected`).

## SHADOW TRADE LIFECYCLE

- CONFIRMED anında, confirming mumun **close fiyatı** = shadow entry
  (gerçek/deterministik confirmation price; CURRENT'ın gelecekteki
  sonucu asla kopyalanmaz — shadow kendi entry'sinden bağımsız simüle
  edilir).
- Aynı `_current_entry_plan()` / `_current_position_transition()`
  fonksiyonları CURRENT ve CONFIRMED_5M_ENTRY_V1 ile paylaşılır: aynı
  stop mesafesi metodolojisi, TP1 +1R (%30), TP2 +2R (%30), runner %40,
  BE stop taşıma, %0.08 taker fee (giriş+çıkış), LONG-only exit
  simülasyonu.
- `_manage_momentum_5m_shadows_once()` her `manage()` çağrısında (scan +
  position_engine, yani ~3-30sn) CONFIRMED durumundaki shadow'ları
  günceller; kapanınca `CLOSED`.
- **PAPER bakiyesine etkisi: SIFIR** — hiçbir yazma `positions`,
  `config` veya bakiye hesaplarına dokunmuyor (test:
  `test_shadow_tp1_tp2_fee_runner_and_zero_paper_or_live_impact` —
  `current_paper_balance()` shadow TP1/TP2/fee sonrası değişmiyor).
- **Gerçek PAPER/LIVE emir: YOK** — `live_fee_tests` tablosu satır
  sayısı shadow işlemlerinden etkilenmiyor; LIVE path'e hiç girilmiyor.

## FAILURE / AUDIT ANALYTICS

- **Candle-level** (`momentum_5m_entry_candle_evaluations`): her
  değerlendirilen tamamlanmış mum için timestamp, OHLC, previous_close,
  o anki breakout_reference, RSI, volume_ratio, move_r ve 7 ayrı
  pass/fail bayrağı + `confirmed_this_candle`. Candidate başına sınırlı
  (~≤6 satır / 30dk pencere), sınırsız büyümüyor.
- **Candidate-level** (`/api/momentum-5m-entry` → `failure_analytics`):
  candle-level loglardan OR ile toplanan "en az bir kez sağlandı mı"
  oranları (`bullish/previous_close/breakout/rsi/volume/min_move/chase
  /all_conditions_together`) — bir candidate'ın çok sayıda mumu, tek bir
  "hiç sağlanmadı" sayacını yanıltıcı şekilde şişirmiyor (OR mantığı,
  candidate başına en fazla 1 kez sayılır). Test:
  `test_matched_cohort_and_failure_analytics`.
- Bu ayrım, spec'in "62 candidate / 0 confirmation" sorununun neden
  tekrar yaşanıp yaşanmadığını görünür kılmak için özellikle tasarlandı.

## DASHBOARD / API

- Yeni sekme: **"12 · MOMENTUM 5M ENTRY"** — mevcut "11 · ENTRY TIMING
  LAB" sekmesi hiç değiştirilmedi.
- Kartlar: Candidates, Pending, Confirmed (+ Confirmation Rate), Timeout,
  Chased Expired, Avg Confirmation Delay, Avg Confirmation Move R.
- Failure Analytics tablosu (candidate-bazlı, 7 koşul + "tüm şartlar
  birlikte").
- **Matched CURRENT vs MOMENTUM_5M_ENTRY_V1** tablosu: `current_matched`
  yalnız MOMENTUM'un kendi CLOSED eşleşmiş `position_id` seti ile
  filtrelenir. Dashboard'da açık uyarı metni var: *"CONFIRMED_5M_ENTRY_V1
  sekmesindeki CURRENT satırıyla AYNI örneklem değildir, doğrudan
  kıyaslanmamalıdır."* — Bu, cohort integrity'i korumak için bilinçli bir
  tasarım kararı: iki model tek bir 3 satırlı tabloda harmanlanmak yerine,
  her biri kendi bağımsız eşleşmiş çiftinde (CURRENT-matched-to-model vs
  model) gösteriliyor; her iki sekme de aynı anda görünür durumda, ama
  örneklemler asla karıştırılmıyor.
- Sample yoksa (`closed_matched_count==0`) tablo ve delta alanı
  **"N/A · henüz kapanmış eşleşmiş sample yok"** gösterir; fake data
  üretilmez.
- Candidates tablosu CONFIRMED_5M_ENTRY_V1 ile aynı kolon setini
  kullanır (id/CURRENT, created/confirmed, symbol, status, original/
  confirm price, delay, 5m OHLC, prev/RSI/vol, move, stop, TP1/TP2,
  MAE/MFE, exit, gross/fee/net).

## CURRENT UNCHANGED

Doğrulandı: `_current_entry_plan`, `_current_position_transition`,
`paper_open` gating sırası, `settings`/`config` tablosu, position
lifecycle kodu **hiç değiştirilmedi** — yalnızca `paper_open`'ın write()
transaction'ına tek satır ek çağrı (`_insert_momentum_5m_candidate`)
eklendi (aynı yerde, `_insert_entry_timing_candidate` ile yan yana).
Regresyon testleri (aşağı) bunu doğruluyor.

## V1 UNCHANGED

`ENTRY_QUALITY_FILTER_V1`, `_long_entry_quality_reasons`,
`process_paper_signal` mantığı dokunulmadı. Deploy sonrası
`/api/health` → `mode=PAPER`, scanner/position_engine hatasız; V1'in
CURRENT LONG gating'i değişmedi.

## V2 / V3 UNCHANGED

`entry_quality_filter_v2_research` / `v3_research` tabloları, insert/sync
fonksiyonları hiç değiştirilmedi; `tests/test_entry_quality_v2_research.py`
ve `test_entry_quality_v3_research.py` aynen geçti (bkz. TESTS).

## EXISTING TIMING LAB (CONFIRMED_5M_ENTRY_V1) UNCHANGED

`entry_timing_lab_candidates` şeması, `_entry_timing_evaluate`,
`_update_entry_timing_candidate`, `manage_entry_timing_candidates`,
`/api/entry-timing-lab` **hiç değiştirilmedi**. Deploy sonrası canlı
kontrol: `total_candidates=62, timeout_count=47` — görev metnindeki
baseline ile birebir eşleşiyor, dokunulmadığı teyitli.

## PAPER BALANCE UNAFFECTED

`test_shadow_tp1_tp2_fee_runner_and_zero_paper_or_live_impact` ve canlı
health kontrolüyle doğrulandı: momentum shadow trade'leri
`current_paper_balance()`'ı hiçbir aşamada değiştirmiyor.

## LIVE DISARMED

Deploy sonrası `/api/live-fee-test/status` → `status=COMPLETED` (id=5,
tek kullanımlık, MEXC_GOREV_01'de tespit edilen durumla aynı, yeniden arm
edilemez). Bu görevde LIVE arm/execute/order işlemi yapılmadı; yeni model
hiçbir LIVE path'e dokunmuyor.

## TESTS

- Yeni dosya: `tests/test_momentum_5m_entry.py` — **20 test**, hepsi
  geçti. Kapsam: CURRENT unchanged + LONG-only eligible cohort, SHORT no
  candidate, duplicate protection + no historical backfill, tüm 7 koşulun
  (parametrize) bağımsız pass/fail'i, completed-candles-only/no-lookahead,
  breakout reference genişlemesi + ilk mumun kendi high'ını kullanmaması,
  preliminary chase/timeout, CANCELLED_INVALID, atomic/idempotent terminal
  geçiş, orijinal R paydasının CURRENT stop hareketinden etkilenmemesi,
  shadow TP1/TP2/fee/runner + sıfır PAPER/LIVE etkisi, restart recovery +
  migration idempotency + mevcut açık pozisyonların etkilenmemesi, matched
  cohort + failure analytics doğruluğu.
- Tam regresyon: `.venv/bin/python -m pytest tests/ -q` →
  **145 passed, 1 xfailed** (125 eski + 20 yeni; eski suite'te hiçbir
  regresyon yok).
- `.venv/bin/python -m py_compile app.py` → **SYNTAX OK**.
- Deploy öncesi ayrıca: production `trader.db`'nin izole bir kopyası
  üzerinde migration + `/api/momentum-5m-entry` end-to-end doğrulandı
  (525 pozisyon korunuyor, integrity ok, `total_candidates=0`, params
  snapshot doğru) — canlı sisteme dokunmadan.

## DEPLOY / VERIFY

- Kullanıcı onayıyla `sudo systemctl restart mexc-trader` çalıştırıldı
  (kullanıcı tarafından, `!` ile bu oturumda).
- **Deploy timestamp:** UTC `2026-09-17T18:49:03Z` ·
  Europe/Istanbul `2026-09-17T21:49:03+03:00`.
- Doğrulama (restart sonrası ilk scan tamamlandıktan sonra):
  - `systemctl is-active mexc-trader` → **active**, `is-enabled` → **enabled**.
  - `/api/health` → `service_status=healthy`, `mode=PAPER`,
    `mexc_api_connection=BAĞLI`, `scanner.alive=true running=true
    last_error=null`, `position_engine`/`ghost_analyzer` aynı şekilde
    hatasız.
  - `PRAGMA integrity_check` (canlı DB, read-only) → **ok**.
  - Açık PAPER pozisyonları: **8/8 korundu** (aynı ID'ler, `status='OPEN'`).
  - `/api/entry-timing-lab` → 62 candidate / 47 timeout (değişmedi).
  - `/api/momentum-5m-entry` → çalışıyor, `total_candidates=0`
    (no-historical-backfill nedeniyle beklenen), params snapshot doğru.
  - `/api/live-fee-test/status` → `COMPLETED` (LIVE disarmed).

## FIRST CANDIDATE COUNT

**0** — beklenen ve normal (görev şartı: no historical backfill; yalnız
deploy sonrası yeni CURRENT LONG fırsatları candidate üretecek).

## DB INTEGRITY

Migration öncesi backup: **ok**. Migration sonrası (izole kopya): **ok**.
Deploy sonrası canlı DB: **ok**. Hiçbir aşamada `DROP/DELETE/UPDATE/
INSERT (ad-hoc)/VACUUM/RESET/REPAIR/BACKFILL` çalıştırılmadı.

## GIT/GITHUB

- Commit edilen dosyalar: `app.py`, `templates/index.html`,
  `tests/test_momentum_5m_entry.py`, `MEXC_GOREV_02_SONUC.md`.
- `trader.db`, `backups/`, `.env`, `secrets`, `credentials` → staged
  değil (commit öncesi `git status` ile doğrulandı).
- Commit + `git push origin main` bu raporun hemen ardından yapıldı;
  kesin commit hash'i ve push sonucu, görevin chat özetinde ve bu
  bölümün push-sonrası halinde raporlanır.

## WARNINGS / LIMITATIONS

- `momentum_5m_entry_candidates` şemasında `shadow_stop_price` adlı bir
  kolon var (CONFIRMED_5M_ENTRY_V1 ile şema simetrisi için eklendi) ama
  hiçbir zaman yazılmıyor — dashboard ve tüm okuma yolları bunun yerine
  doğru şekilde güncel değeri taşıyan `shadow_current_stop`'u kullanıyor.
  Kolon inert/NULL kalıyor; fonksiyonel bir etkisi yok, yalnızca kullanılmayan
  bir alan. Gelecekte temizlenebilir ama additive-only kural gereği şimdi
  silinmedi.
- `MEXC_GOREV_01_SONUC.md` dosyasının çalışma dizininden bu oturumun
  dışında (ne MEXC_GOREV_01'de ne de bu görevde benim tarafımdan) silinmiş
  olduğu fark edildi; git geçmişinde hâlâ mevcut, bu görevin kapsamı
  dışında olduğu için dokunulmadı, kullanıcıya ayrıca bildirilecek.
- MEXC breakout tanımı, candidate oluşumundaki CURRENT giriş fiyatında
  çapalanan ve genişleyen bir "running high" olarak yorumlandı (spec'in
  "candidate oluşturulduğundan beri daha önce tamamlanmış candle'ların
  high referansı" ifadesi için en tutarlı, look-ahead-free, ek API çağrısı
  gerektirmeyen yorum). Bu tasarım kararı "PARAMETER / VERSION SNAPSHOT"
  bölümünde `params_json.breakout_definition` alanına açıkça yazıldı.
- Gerçek piyasa koşullarında ilk confirmation/timeout/chase istatistikleri
  birkaç gün/hafta toplanmadan yorumlanmamalı (görev talimatı: küçük
  örneklemden kesin sonuç çıkarılmayacak).

## OPEN ITEMS

- MOMENTUM_5M_ENTRY_V1 forward veri toplamaya devam edecek; CURRENT'a
  taşınması veya V2 açılması ayrı, açıkça talep edilmiş bir görev
  gerektirir.
- Yeterli closed sample oluştuğunda (birkaç düzine confirmed+closed
  candidate) matched-cohort tablosu anlamlı hale gelecek; şu an N/A.
- `MEXC_GOREV_01_SONUC.md`'nin çalışma dizininden silinmiş olması
  kullanıcıya bildirilmeli (git history'de kayıp değil).

## MOMENTUM_5M_ENTRY_V1'İ CURRENT'A AKTİF ETME UYARISI

Bu model **research-only** olarak deploy edildi. CURRENT entry/risk/
sizing/stop/TP/runner/fee mantığına hiçbir şekilde bağlanmadı ve bu
görevde CURRENT'a aktif edilmedi.
