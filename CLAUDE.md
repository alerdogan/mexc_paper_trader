# MEXC_PAPER_TRADER — Proje Anayasası (CLAUDE.md)

PROJECT_ID: MEXC_PAPER_TRADER
PROJECT_ROOT: /opt/mexc_paper_trader
REMOTE: git@github.com:alerdogan/mexc_paper_trader.git
BRANCH: main
PORT: 8071
SERVICE: mexc-trader.service (systemd, user=pronist)

Bu dosya, MEXC_GOREV_01 (Claude Project Takeover) görevi sırasında proje
incelenerek oluşturulmuştur. Bundan sonraki tüm MEXC_GOREV_XX görevlerinin
uyması gereken kalıcı kurallardır. `AGENTS.md` içindeki güvenlik kuralları bu
dosyanın **üzerine değil, yanına** eklenmiştir — çelişki halinde daha sıkı
olan kural uygulanır.

---

## A) PROJECT ISOLATION

- Bu oturumlar SADECE `/opt/mexc_paper_trader` içindir.
- `/home/pronist/pro-portal` ayrı bir projedir: girilmez, okunmaz,
  değiştirilmez. İki proje arasında kod/config/database/git bilgisi
  taşınmaz.
- Her görev başında kimlik doğrulanır:
  `pwd`, `git rev-parse --show-toplevel`, `git branch --show-current`,
  `git remote get-url origin`. Beklenenle uyuşmuyorsa göreve devam
  edilmez, durum raporlanır.

## B) PAPER / LIVE SAFETY

- **CURRENT çalışma modu PAPER'dır** ve varsayılandır. `app.py` içinde
  `PAPER_AUTO_START=True`; servis her başladığında (`@app.on_event('startup')`)
  yalnız PAPER motoru (`scanner`, `position_engine`, `ghost_analyzer`)
  otomatik başlar.
- **LIVE hiçbir zaman otomatik başlamaz.** Gerçek emir gönderen tek yol
  `LIVE_FEE_TEST` akışıdır (`/api/live-fee-test/*`): yalnızca
  `127.0.0.1`/`::1`'den (`_require_local`), çok aşamalı arm → prepare →
  execute + ayrı bir onay metni ile çalışır, tek kullanımlıktır (bir
  `COMPLETED` kaydından sonra bir daha arm edilemez). Takeover anında
  DB'de zaten 1 `COMPLETED` kayıt (id=5) var; bu yüzden bu akış fiilen
  kapalıdır.
- Kullanıcı açıkça istemedikçe: LIVE enable/arm/execute etmek, LIVE emir
  göndermek veya bu akışla ilgili kod/güvenlik mantığını gevşetmek
  YASAKTIR.
- `credential_get()` önce ortam değişkenini (`MEXC_API_KEY`,
  `MEXC_API_SECRET`, systemd `EnvironmentFile=-/etc/mexc-trader/secrets.env`
  üzerinden) okur, yalnız `darwin` platformda macOS Keychain'e düşer. Linux
  production'da Keychain kullanılmaz — bu davranış korunmalı.

## C) DATABASE SAFETY

- `trader.db` **silinmez, yeniden oluşturulmaz, sıfırlanmaz.**
- Trading ve research geçmişi (`positions`, `paper_epochs`,
  `position_score_snapshots`, `market_regime_snapshots`,
  `entry_filter_rejections`, `reject_shadow_trades`,
  `entry_quality_filter_v2_research`, `entry_quality_filter_v3_research`,
  `entry_timing_lab_candidates`, `strategy_lab_experiments`,
  `strategy_lab_runs`, `live_fee_tests`, `logs`) korunur.
- Migration'lar additive/idempotent olmalı (bkz. `init_db()` — mevcut
  kod zaten `ALTER TABLE ... ADD COLUMN` desenini try/except ile
  idempotent uyguluyor; bu desen korunmalı).
- Şema veya veri riski taşıyan her işlemden önce bütünlüğü doğrulanmış,
  zaman damgalı yedek alınır (`backups/` dizini örnek deseni izler:
  `trader-YYYYMMDDTHHMMSSZ.db`). Yedek sonrası `PRAGMA integrity_check`
  çalıştırılır.
- Bu projede ASLA (kullanıcı açık onayı olmadan) çalıştırılmaz: `DROP`,
  `DELETE`, `UPDATE`, `INSERT` (manuel/ad-hoc), `VACUUM`, reset, repair,
  backfill/fake-data.
- Testler `tests/conftest.py` içindeki `forbid_production_database`
  fixture'ı ile prod `trader.db`'ye bağlanmayı otomatik engeller; bu
  korumayı kaldırmayın veya bypass etmeyin.

## D) RESEARCH INTEGRITY

- Sahte/geçmişe dönük backfill yapılmaz; gelecekteki bilgi (future
  information) kullanılmaz.
- Küçük örneklemle CURRENT strateji değiştirilmez.
- Research modelleri (V2, V3, Entry Timing Lab, Strategy Lab modelleri)
  otomatik olarak CURRENT yapılmaz — CURRENT'a geçiş yalnızca kullanıcının
  açık kararıyla, ayrı bir görevde yapılır.
- Matched cohort karşılaştırmaları tercih edilir, overfitting'den kaçınılır.
- Mevcut research mimarisi (aşağıdaki "RESEARCH SİSTEMLERİ" bölümüne
  bakın) shadow/gözlemsel olacak şekilde tasarlanmıştır; bu ayrımı bozacak
  değişiklik yapılmaz.

## E) EXISTING STRATEGY PROTECTION

Görev açıkça istemedikçe CURRENT parametreler değiştirilmez. DB `config`
tablosundaki **fiilen aktif** değerler (kod içi `DEFAULTS` değil):

| Parametre | Değer |
|---|---|
| leverage | 3 |
| risk_per_trade_usd | $20 |
| max_alt_notional_usd | $2000 |
| max_btc_notional_usd | $10000 |
| max_total_open_risk_usd | $500 |
| daily_loss_limit_usd | $500 |
| min_free_balance_pct | %20 (serbest bakiye rezervi) |
| signal_threshold | 80 |
| stop_atr_mult | 1.5 ATR |
| tp1_r / tp1_pct | 1.0R / %30 |
| tp2_r / tp2_pct | 2.0R / %30 |
| runner_pct | %40 |
| move_be_at_r | 1.0R (stop break-even'a çekilir) |
| paper_fee_rate | %0.08 (taker) |
| paper_balance (başlangıç) | $4000 |
| scan_seconds | 30 |
| universe | Top-30 hacim + MEXC Spot USDT crypto whitelist |
| timeframe'ler | 15m (sinyal), 1h, 4h (trend teyidi) |

Position lifecycle: `paper_open` → TP1 (%30 kapanış + stop BE) → TP2
(%30 kapanış) → runner (%40, `TRAILING_RUNNER` modelinde
`max_favorable_R - 1R` trailing stop) → `close_reason` ile kapanış
(`STOP`/`MANUAL`/`EMERGENCY_CLOSE`/`RUNNER_TRAILING_STOP` vb.).

Bu değerler ve entry/exit/stop/TP/runner/fee mantığı, görev açıkça
istemedikçe değiştirilmez. V1/V2/V3/Strategy Lab/Entry Timing Lab gibi
mevcut araştırmalar da görev açıkça istemiyorsa değiştirilmez.

### RESEARCH SİSTEMLERİ (mevcut durum)

| Sistem | Durum | CURRENT'a etkisi |
|---|---|---|
| `ENTRY_QUALITY_FILTER_V1` | **ACTIVE** | Evet — yalnız yeni LONG sinyallerini engeller (`process_paper_signal`). Kurallar: coin RSI≥64, BTC/ETH 15m volume ratio<0.55, coin LONG volume_score=0. SHORT etkilenmez. Reddedilenler `entry_filter_rejections` + `reject_shadow_trades`'e kaydedilir. |
| `PAPER_CURRENT_V1_ACTIVE` epoch | **ACTIVE** | Evet — aktif PAPER epoch (id=1), 2026-09-08'den itibaren temiz baseline. |
| `ENTRY_QUALITY_FILTER_V2_RESEARCH` | RESEARCH (shadow) | Hayır — `paper_open` içinde gerçek pozisyon açıldıktan SONRA kaydedilir, gating yapmaz. |
| `ENTRY_QUALITY_FILTER_V3_RESEARCH` | RESEARCH (shadow) | Hayır — aynı şekilde non-blocking. |
| Strategy Lab (`CURRENT`,`NO_STOP_MINI`,`SMART_EXIT`,`TRAILING_RUNNER`) | RESEARCH (shadow) | Hayır — ana bakiye/risk/pozisyon tablolarını etkilemez, sadece paralel simülasyon. |
| Stop Analyzer / ghost analiz | RESEARCH | Hayır — stop sonrası 2 saatlik ghost MAE/MFE takibi, geriye dönük analiz; trading kararını etkilemez. |
| Score snapshot / market regime snapshot | RESEARCH (immutable) | Hayır — giriş anında sabitlenen ham veri; score algoritmasını değiştirmez. |
| `CONFIRMED_5M_ENTRY` (Entry Timing Lab, `ENTRY_TIMING_LAB_VERSION='CONFIRMED_5M_ENTRY_V1'`) | RESEARCH (shadow) | Hayır — `_insert_entry_timing_candidate` gerçek pozisyon açıldıktan sonra çağrılır; CURRENT işlemi değiştirmez, yalnız paralel R/timing simülasyonu üretir. |
| `LIVE_FEE_TEST` | Tek seferlik, tamamlandı (id=5, COMPLETED) | Hayır — CURRENT PAPER akışından tamamen ayrı, tekrar arm edilemez. |

Bir research sistemi CURRENT'a taşınacaksa (V1→sonraki versiyon,
V2/V3→CURRENT, Entry Timing Lab→CURRENT execution vb.) bu **ayrı, açıkça
talep edilmiş bir görev** olmalı; sonuç dosyasında eski/yeni karşılaştırması
belgelenmeli.

## F) SECRETS

- Secret/API key/password/token hiçbir zaman log, sonuç dosyası, commit
  veya chat çıktısına yazılmaz.
- Gerçek secret dosyası `/etc/mexc-trader/secrets.env` içindedir (repo
  dışında, izinle korunur — bu oturumda erişim denenmemeli/gerekmez).
- `secrets.env.example`, `deploy/mexc-trader-credentials.conf` yalnızca
  placeholder/pointer içerir; bunlar tracked kalabilir, içine gerçek
  değer yazılmaz.
- `.gitignore` zaten `trader.db`, `backups/`, `.env*`, `secrets.env*`,
  `*.key/*.pem/*credentials*` gibi dosyaları hariç tutuyor; bu kapsam
  daraltılmaz.

## G) TEST / DEPLOY / GIT WORKFLOW

Kod değişikliği içeren normal görevlerde standart akış:

inspect → (gerekiyorsa) backup → implement → tests → deploy → verify →
commit → `git push origin main` → sonuç raporu.

- Test: `.venv/bin/python -m pytest tests/ -q` (takeover anında 125
  passed, 1 xfailed). `tests/conftest.py` prod DB'ye bağlanmayı engeller.
- Syntax: `.venv/bin/python -m py_compile app.py`.
- Deploy sonrası doğrulama: `curl -s http://127.0.0.1:8071/api/health`
  ve `/api/status` ile `service_status`, `mode`, `scanner.running`,
  `position_engine.running` kontrol edilir.
- Push öncesi `git status` ile DB/secrets/backups'ın staged olmadığı
  doğrulanır.
- Servis restart/reload gerekiyorsa (`systemctl restart mexc-trader`)
  bu **onay gerektiren** bir işlemdir (bkz. AGENTS.md ve bölüm H).

## H) APPROVAL / USER INTERRUPTION POLICY

`AGENTS.md` içindeki "Approval Policy" bölümü birebir geçerlidir. Özet:

Onay GEREKMEZ: dosya okuma/arama/düzenleme, test çalıştırma, `git status`/
`diff`/`add`/`commit`, localhost curl/health kontrolü, normal Python/
FastAPI/UI geliştirme, log inceleme, read-only DB sorguları, proje içi
güvenli backup, proje içi güvenli build/deploy hazırlığı, `git push origin
main`.

Onay GEREKİR: `sudo`, `systemctl` restart/stop/start veya systemd
değişikliği, OS/paket kurulumu-kaldırma, `trader.db` silme/sıfırlama/manuel
veri güncelleme, destructive/irreversible DB migration, credential
görüntüleme/değiştirme, gerçek LIVE emir/LIVE davranış değişikliği,
`git reset`/`revert`/`checkout` ile mevcut işi geri alma, dosya silme, geri
dönüşü olmayan/veri kaybı riski taşıyan her işlem, kapsamı ciddi biçimde
aşan beklenmedik durumlar.

Belirsizlik küçükse mevcut kod, `AGENTS.md`, bu dosya ve görev
dokümanından güvenli varsayım çıkarılıp ilerlenir; gereksiz tasarım/onay
sorularıyla kullanıcı bölünmez.

## I) RESULT FILE PROTOCOL

Her `MEXC_GOREV_XX` görevinin sonunda
`/opt/mexc_paper_trader/MEXC_GOREV_XX_SONUC.md` oluşturulur. En az: görev
no, tarih, amaç, başlangıç HEAD, yapılan inceleme/değişiklik, değişen
dosyalar, DB değişikliği, backup, tests, deploy, service/health/mode/
scanner durumu, PAPER/LIVE durumu, DB integrity, commit, GitHub push,
warnings, açık kalan işler bulunur. Görev yalnız audit ise uygulanmayan
bölümlere N/A yazılır.

---

## MİMARİ ÖZET (referans)

- **Tek dosya FastAPI uygulaması**: `app.py` (~2525 satır). `templates/index.html`
  tek sayfa dashboard (Jinja2 + vanilla JS).
- **DB**: SQLite (`trader.db`), WAL modu, `busy_timeout=30000`,
  `_sqlite_write_with_retry` ile yazma retry'ı.
- **Background task'lar** (`TASK_NAMES=('scanner','position_engine','ghost_analyzer')`):
  `supervise_task()` ile supervised — hata alırsa `SUPERVISOR_BACKOFF=(1,2,5,10,30)`
  saniyelik artan gecikmeyle otomatik yeniden başlar. `start_background_tasks()`
  zaten çalışan (`not done()`) bir task'ı tekrar başlatmaz → **duplicate
  scanner/worker koruması** budur.
- **Startup/recovery**: `@app.on_event('startup')` → `init_db()` (idempotent
  ALTER TABLE'lar) → `PAPER_AUTO_START` ise `state.running=True` → task'lar
  başlar. Açık PAPER pozisyonları DB'de kalıcıdır (`status='OPEN'`); servis
  yeniden başladığında `position_engine` bunları DB'den okuyup yönetmeye
  devam eder (kod içinde ayrı bir "recovery" adımı yok, çünkü state zaten
  DB'de tutuluyor — restart-safe tasarım).
- **Risk/position sizing**: `_current_entry_plan()` stop mesafesini ATR'den
  (`stop_atr_mult`), pozisyon büyüklüğünü sabit `risk_per_trade_usd`'yi stop
  mesafesine bölerek hesaplar; `paper_open()` içinde toplam açık risk, günlük
  zarar limiti, PAPER bakiye ve margin kontrolleri sırayla uygulanır.
- **Dashboard/API**: `/api/health`, `/api/status`, `/api/history`,
  `/api/entry-filter-rejections`, `/api/entry-timing-lab`, `/api/strategy-lab`,
  `/api/stop-analysis`, `/api/live-fee-test/*`, pozisyon kapatma ve toplu
  kapatma (`/api/positions/close-all*`) uçları.
- **systemd**: `mexc-trader.service`, `User=pronist`, `WorkingDirectory=/opt/mexc_paper_trader`,
  `ExecStart=.venv/bin/python -m uvicorn app:app --host 0.0.0.0 --port 8071`,
  `Restart=always`, `EnvironmentFile=-/etc/mexc-trader/secrets.env`.

Bu bölüm bilgi amaçlıdır; kod değiştikçe güncelliğini yitirebilir — kesin
kaynak her zaman kod ve DB'nin kendisidir.
