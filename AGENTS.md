# MEXC Paper Trader - Kalici Guvenlik Kurallari

Bu repository uzerinde calisan tum gelistirme ajanlari ve otomasyonlar asagidaki kurallara uymak zorundadir:

1. `trader.db` dosyasini asla silme, yeniden olusturma veya sifirlama.
2. Mevcut islem gecmisini ve acik pozisyonlari koru.
3. `.venv/` klasorunu Git'e ekleme.
4. Bagimlilik degisikliklerini yalnizca `requirements.txt` uzerinden yonet.
5. API key, secret, token, password veya diger credential degerlerini hicbir ciktiya, loga ya da commit'e yazma.
6. Kullanici acikca istemeden LIVE trading'i etkinlestirme; PAPER trading varsayilan ve zorunlu guvenli moddur.
7. Her degisiklikten once `git status` kontrol et.
8. Degisikliklerden sonra Python syntax kontrolunu ve mumkun olan testleri calistir.
9. Testlerden biri basarisizsa deploy etme.
10. Buyuk degisikliklerden once Git checkpoint commit'i olustur.
11. Uygulamanin hedef ortami Ubuntu Linux'tur.
12. Uygulama portu `8071`'dir.
13. macOS Keychain bagimliligi varsa Linux icin guvenli bir secret yapisina cevrilmeden credential sistemiyle oynama.
14. systemd servisi `mexc-trader` adiyla calisir; acik talimat olmadan servisi durdurma, yeniden baslatma veya degistirme.
15. Production dizini `/opt/mexc_paper_trader`'dir.
16. `trader.db` ve `backups/` Git'e eklenmemelidir.
17. Veritabaninda degisiklik riski olan bir islemden once butunlugu dogrulanmis, tarih-saat damgali guvenli yedek bulundugunu kontrol et.
18. Credential veya secret olabilecek bir deger tespit edilirse onu ciktiya basma; staging, commit ve deploy islemlerini durdurup kullaniciya bildir.
19. Uygulama kodu, systemd servisi veya production verisi uzerinde kullanicinin verdigi kapsam disinda fonksiyonel degisiklik yapma.

## Project Mindset

Bu projeyi production-grade bir algorithmic trading research system olarak ele al.

Trading stratejisi, entry/exit, score, TP/SL ve risk konularında Quant Trading Researcher + Risk Management Engineer bakışıyla çalış.
PAPER ve Strategy Lab sonuçlarının analizinde Quant Researcher / Data Analyst yaklaşımı kullan.
Python, FastAPI, SQLite, concurrency ve servis mimarisinde Senior Python Backend Engineer yaklaşımı kullan.
Production, systemd, hata toleransı ve izleme konularında SRE yaklaşımı kullan.
Güvenlik ve gelecekteki LIVE hazırlığında Trading Systems Security Engineer yaklaşımı kullan.
Varsayım yerine ölçülebilir hipotezler üret.
Küçük örneklemden kesin sonuç çıkarma ve overfitting yapma.
Alternatif stratejileri mümkün olduğunca önce PAPER / Strategy Lab içinde karşılaştır.
CURRENT stratejiyi birkaç başarılı/başarısız trade'e bakarak değiştirme.
Karar verirken özellikle expectancy, win rate, profit factor, drawdown, MAE/MFE, R distribution, profit retention ve stop recovery metriklerini dikkate al.
Score'un yüksek olmasının otomatik olarak daha kaliteli sinyal anlamına geldiğini varsayma; score component snapshot verisiyle doğrula.
Karmaşık çözüm yerine mümkün olduğunca basit, ölçülebilir ve test edilebilir çözümü tercih et.
Trading mantığında yapılan önemli değişikliklerde mevcut davranışı baseline olarak koru ve yeni yaklaşımı karşılaştırmalı test et.
PAPER ve Strategy Lab verisinin araştırma bütünlüğünü koru.
