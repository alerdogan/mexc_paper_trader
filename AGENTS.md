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
