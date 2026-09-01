MEXC Futures Paper Trader — macOS Faz 1.2

1) START_MAC.command dosyasını çift tıklayın.
2) Tarayıcı otomatik olarak http://127.0.0.1:8071 adresini açar.
3) Sistem PAPER/SANAL modundadır; gerçek emir göndermez.
4) Piyasa verisi MEXC Futures public API üzerinden gelir.
5) Sinyal kartlarında LONG/SHORT puan dökümü görünür.
6) 70–79 puan arası sinyaller “Yaklaşan Fırsatlar” bölümünde listelenir.
7) PAPER test için 60/70 geçici eşik butonları vardır. “Normal ayar” düğmesi kalıcı sinyal eşiğine geri döner (varsayılan 80).
8) API Key/Secret girilirse macOS Keychain içinde saklanır. PAPER modunda emir gönderimi yoktur.

## Python surumu
Baslatici Python 3.13, 3.12, 3.11, 3.10 sirasiyla arar. Mevcut `.venv` farkli bir Python ana surumuyle olusturulduysa otomatik olarak yeniden kurar.

## Faz 1.4.1 düzeltmesi
- Top-30 arayüzündeki JavaScript sözdizimi hatası giderildi. Butonlar, Futures bağlantı testi ve Şimdi Tara yeniden çalışır.

## Faz 1.4.2 - CRYPTO ONLY
Top-30 havuzu yalnızca MEXC Spot tarafında gerçek USDT kripto çifti bulunan Futures sözleşmelerinden oluşturulur. Stock Futures, endeks/ETF ve benzeri sentetik ürünler otomatik olarak dışarıda bırakılır.


## Faz 1.8.3
- Her aktif PAPER pozisyon satırının sağında manuel Kapat butonu.
- Kapatma öncesi onay; güncel Futures fiyatıyla gerçekleşen P&L ve otomatik geçmişe taşıma.
- Açık risk kartında yeni işlem için kullanılabilir risk gösterimi.
- Mevcut trader.db paket içinde yoktur; güncellemede eski veritabanını koruyun.


## Faz 1.8.2 düzeltmesi
- Manuel pozisyon kapatma işlemi SQLite eşzamanlı erişimine karşı güçlendirildi.
- WAL ve 15 saniye busy timeout eklendi.
- Ağ isteği sırasında veritabanı bağlantısı açık tutulmuyor.
- Aynı pozisyon otomatik ve manuel olarak aynı anda kapanmaya çalışırsa güvenli biçimde engelleniyor.
- HTTP 500 durumunda arayüz gerçek backend hata mesajını göstermeye çalışıyor.


## Faz 1.8.2 düzeltmesi
- Başlat butonu artık yalnız motor durumunu değiştirmez; anında ilk piyasa taramasını da çalıştırır.
- Şimdi Tara butonu sadece ekstra manuel tarama için kalır.
- Mevcut scan_lock yapısı sayesinde Başlat ve periyodik motor aynı anda ikinci bir tarama başlatmaz.


## Faz 1.8.2
- Geçmiş pozisyonlar artık tam satır detayında görünür.
- Kapanış fiyatı bundan sonraki manuel ve otomatik kapanışlarda veritabanına kaydedilir.
- Geçmiş tabloda coin, yön, skor, giriş, kapanış, son stop, gerçekleşen K/Z, açılış ve kapanış zamanı gösterilir.
- Eski kapanmış kayıtlarda kapanış fiyatı önceden saklanmadığı için giriş fiyatı yedek olarak gösterilir; mevcut K/Z korunur.


## Faz 1.8.2
- `database is locked` kök nedeni düzeltildi.
- SQLite WAL modu artık her bağlantıda değil yalnız uygulama başlangıcında açılır.
- DB busy timeout 30 saniyeye çıkarıldı.
- TP1/TP2/stop yönetiminde log kayıtları ana write transaction kapandıktan sonra yazılır.
- Log yazımı kısa retry kullanır ve trading işlemini kilitlemez.
- Manuel Kapat işlemi eşzamanlı scanner yazımına karşı otomatik retry yapar.


## Faz 1.8.2
- Aktif pozisyon tablosuna TP1 / Kâr Al ve TP2 / Kâr Al fiyat kolonları eklendi.
- TP fiyatları giriş ile ilk stop arasındaki R mesafesinden hesaplanır.
- TP1/TP2 gerçekleştiğinde ilgili fiyatın yanında ✓ görünür.
- Stop kolonu güncel stop seviyesini göstermeye devam eder.


## Faz 1.8.2 — Performans + Hızlı TP/SL Motoru
- Başlat endpoint'i artık ağır taramayı beklemez; anında cevap verir ve ilk taramayı arka planda başlatır.
- Top-30 coin analizi kontrollü paralel çalışır; aynı anda en fazla 6 kline isteği yapılır.
- Top-30 + Spot crypto whitelist sonucu `universe_refresh_minutes` süresince cache edilir.
- Ağır 15m/1h/4h sinyal taraması ile açık pozisyon yönetimi birbirinden ayrıldı.
- Açık pozisyonlar tek Futures ticker isteğiyle yaklaşık her 3 saniyede güncellenir.
- TP1, TP2 ve Stop kontrolleri artık 30 coinlik ağır taramanın bitmesini beklemez.
- PAPER modunda TP1/TP2 gerçek MEXC emri göndermez; sanal kısmi kapanış uygular. LIVE mod ileride reduce-only emirle bağlanacaktır.
- UI'da tarama sırasında `TARANIYOR…`, son tarama süresi ve hızlı fiyat güncelleme zamanı görünür.


## Faz 1.8.2 — Geçmiş Tablo Düzeltmesi
- Geçmiş tablo gövdesindeki `id="history"` tarayıcının yerleşik `window.history` nesnesiyle çakışıyordu.
- Bu nedenle özet K/Z hesapları görünürken kapanmış işlem satırları tabloya yazılmıyordu.
- Tablo ID'si `historyRows` olarak değiştirildi ve JavaScript artık elementi açıkça `document.getElementById()` ile seçiyor.


## Faz 1.8.2 — Pozisyon Büyüklüğü
- Aktif pozisyon tablosuna `Pozisyon $` kolonu eklendi.
- Büyük değer, kalan açık miktarın anlık fiyatla USD notional büyüklüğünü gösterir.
- Alt satırdaki `ilk $...` değeri işlem açıldığı andaki başlangıç notional büyüklüğünü gösterir.
- TP1/TP2 ile kısmi kapanış oldukça üstteki kalan pozisyon büyüklüğü otomatik küçülür.


## Faz 1.8.2 — Geçmiş Toplam K/Z
- Geçmiş Pozisyonlar başlığının sağ tarafına seçili dönemin toplam gerçekleşen K/Z değeri eklendi.
- Bugün / Bu Hafta / Bu Ay / Tümü filtreleri değiştikçe başlık ve toplam değer otomatik güncellenir.
- Pozitif toplam yeşil, negatif toplam kırmızı sınıfıyla vurgulanır.


## Faz 1.8.2 — Gerçek PAPER Bakiye / Equity
- Ayarlardaki `paper_balance` artık başlangıç sermayesidir.
- `Sanal Kasa = başlangıç sermayesi + tüm zamanlarda kapanmış işlemlerin gerçekleşmiş K/Z toplamı`.
- Dün veya daha önce kapanan işlemler de kasaya kalıcı olarak yansır.
- `Tahmini Equity = güncel Sanal Kasa + açık pozisyonlarda gerçekleşmiş kısmi K/Z + açık pozisyonların anlık gerçekleşmemiş K/Z`.
- Günlük/haftalık/aylık/yıllık performans raporları ayrıca korunur.
- Mevcut `trader.db` içindeki eski kapanmış işlemler otomatik olarak yeni bakiye hesabına dahil edilir; veritabanını sıfırlamak gerekmez.


## Faz 1.8.2 — Margin + Yaklaşan Fırsat Temizliği
- Aktif pozisyon tablosuna `Margin $` kolonu eklendi.
- Margin yaklaşık olarak `anlık kalan pozisyon notional / leverage` şeklinde hesaplanır.
- Kolon altında kullanılan kaldıraç ve başlangıç margin değeri de gösterilir.
- TP1/TP2 ile pozisyon küçüldükçe kalan margin otomatik küçülür.
- Açık pozisyonu bulunan coin artık `Yaklaşan Fırsatlar` listesinde gösterilmez.
- Pozisyon kapanır ve coin hâlâ 70–79 skor aralığındaysa tekrar Yaklaşan Fırsatlar'a girebilir.


## Faz 1.8.2 — Stop % Kolonu
- Aktif pozisyon tablosuna `Stop %` kolonu eklendi.
- Değer, giriş fiyatı ile başlangıç stop fiyatı arasındaki mesafenin giriş fiyatına oranıdır.
- Böylece sabit dolar riski korunurken pozisyon büyüklüklerinin neden farklı olduğu doğrudan satırdan anlaşılır.


## Faz 1.8.2 — PAPER Bakiye / Margin Güvenliği
- PAPER günlük zarar limiti varsayılanı `$300` yapıldı; aynı anda açık risk limiti `$100` olarak korunur.
- Yeni işlemden önce gerçek PAPER bakiye kontrol edilir.
- PAPER bakiye `0` veya altına düşerse yeni pozisyon açılmaz; açık pozisyonların TP/SL yönetimi çalışmaya devam eder.
- `Kullanılabilir Margin = güncel Sanal Kasa - açık pozisyonlarda kullanılan margin - rezerv`.
- Varsayılan serbest bakiye rezervi `%20` olarak eklendi (`min_free_balance_pct`).
- Yeni pozisyonun gereken margini kullanılabilir marginden büyükse işlem açılmaz ve log'a `RISK` kaydı düşer.
- Sanal Kasa kartında kullanılabilir margin ve rezerv yüzdesi gösterilir.


## Faz 1.8.2 — Fee Takibi
- Yeni PAPER işlemlerinde %0,08 taker fee simülasyonu.
- Giriş, TP1, TP2 ve son çıkış fee'leri birikir.
- P/L fee sonrası nettir.
- Geçmiş Pozisyonlar: Fee ve Net K/Z kolonları.
- Eski işlemler geçmiş fee kaydı olmadığı için $0 görünür.


## Faz 1.8.2 — MEXC Rate Limit Koruması
- Tarayıcı, hızlı açık pozisyon fiyat takibi, Public API Test ve Private API Test tek bir global MEXC request gate kullanır.
- MEXC istekleri minimum 160 ms aralıkla gönderilir; ani request burst engellenir.
- HTTP 429 veya MEXC `code=510 / Requests are too frequent` gelirse otomatik 1s, 2s, 4s, 8s bekleyerek yeniden denenir.
- Kline eşzamanlılığı 6'dan 4'e düşürüldü.
- Rate-limit beklemeleri log ekranında WARN olarak görünür.
- Tek bir 510 cevabı artık coin taramasını hemen hata olarak bırakmaz; retry zinciri bittikten sonra ancak gerçek hata sayılır.


## Faz 1.8.2 — UI State Indicators
- Üstte sürekli görünen BOT DURUMU, MEXC FUTURES, TARAMA ve SON TARAMA durum şeridi eklendi.
- BAŞLAT aktifken yeşil/vurgulu görünür ve tekrar basılamaz.
- Motor çalışırken DURDUR kullanılabilir; motor durmuşken DURDUR pasif ve aktif-durum vurgusu gösterir.
- Tarama sırasında ŞİMDİ TARA butonu pasifleşir ve mavi aktif görünür.
- ACİL DURDUR basıldığında kırmızı aktif durum gösterilir; diğer işlem kontrolleri güvenli biçimde pasifleşir.
- MEXC bağlantısı BAĞLI / HATA / TEST EDİLMEDİ olarak renkli rozet mantığıyla gösterilir.
- Rate-limit backoff sırasında `MEXC BEKLENİYOR · N sn` görünür.
- Mobil/dar ekranlarda durum kartları ve kontroller responsive yerleşir.
- Geçmiş tablosundaki Fee / Net K/Z başlığı, 1.7.8 fee satır yapısıyla hizalandı.


## Faz 1.8.2 — STOP ANALYZER
- Stop mesafesi DEĞİŞTİRİLMEDİ; varsayılan 1.5 ATR aynen korunur.
- Yeni PAPER işlemlerde MAE (aleyhte maksimum hareket) ve MFE (lehte maksimum hareket) R cinsinden canlı kaydedilir.
- Güncelleme sırasında açık olan eski işlemler için MAE/MFE takip başlangıcı güncelleme anıdır; önceki hareket bilinmediği için bu satırlar kısmi canlı veri sayılır.
- Sistem tarafından stopla kapanan yeni işlemler `close_reason=STOP`, elle kapatılanlar `MANUAL` olarak işaretlenir.
- Yeni `5 · Stop Analizi` sayfasında stop sonrası 2 saatlik hayalet takip gösterilir.
- `Geçmiş Stopları Analiz Et` düğmesi eski kapanmış işlemlerde muhtemel stopları tespit eder ve MEXC 15m geçmiş mumlarından yaklaşık MAE/MFE + 2 saat ghost analizini üretir.
- Stop sonrası girişe göre +1R görülmüşse `MUHTEMEL ERKEN STOP`, +2R görülmüşse `ÇOK MUHTEMEL ERKEN STOP` olarak işaretlenir.
- Ghost sonuç 2 saat dolmadan geçicidir; arka plan motoru her 5 dakikada eksik stop analizlerini günceller.
- Geçmiş analiz 15m OHLC kullandığı için mum içi sıralamayı bilemez; bu nedenle eski işlemlerde sonuç yaklaşık, yeni canlı MAE/MFE verisi daha hassastır.


## Faz 1.8.2 — COMPACT UI
- PAPER / SANAL rozeti yaklaşık %50 küçültüldü.
- Pozisyonlar sayfasındaki tabloların fontu yaklaşık %30 küçültüldü, hücre paddingleri daraltıldı.
- Pozisyonlar sayfası daha fazla bilgiyi tek ekrana sığdıracak şekilde sıkıştırıldı.
- Piyasa Tarayıcı daha kompakt hale getirildi.
- Detaylı analizler sürekli açık göstermek yerine Detay düğmesiyle açılır/kapanır hale getirildi.
- Analiz verileri kaldırılmadı; sadece gerektiğinde görünür hale getirildi.


## Faz 1.8.3 — CASH LEDGER FIX
- Açık pozisyonda TP1/TP2 ile realize edilen satış kâr/zararı Sanal Kasa'ya anında yansır.
- Giriş ve çıkış fee'leri de realize edildiği anda kasaya yansır.
- Tam kapanmış işlemlerin net K/Z'si aynı şekilde kasada kalır.
- Tahmini Equity hesabında açık pozisyonun realize edilmiş kısmının iki kez sayılması engellendi.
- Kullanılabilir margin hesabı güncel realize edilmiş kasa üzerinden hesaplanır.

## Strategy Lab — SHADOW MODEL KARŞILAŞTIRMASI
- Her yeni PAPER girişi CURRENT, NO_STOP_MINI ve SMART_EXIT modellerinde aynı anda, tamamen sanal olarak izlenir.
- Lab kayıtları ana PAPER bakiye, risk ve pozisyon tablolarını etkilemez.
- CURRENT gerçek PAPER davranışını referans alır.
- NO_STOP_MINI stopta kalan miktarın %85'ini kapatır, %15 mini kısmı en fazla 2 saat izler ve -2R sert sınır uygular.
- SMART_EXIT stop temasından sonra 15 dakika teyit bekler ve -1,5R sert sınır uygular.
- Strategy Lab ekranı modellerin K/Z, başarı, profit factor, MAE/MFE ve CURRENT stopundan sonra kâra dönüş sayılarını yan yana gösterir.
- Kâra dönüş, CURRENT stop zamanından sonra fee dahil net K/Z'nin sıfırın üzerine çıkması olarak ölçülür.
