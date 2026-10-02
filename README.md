[README.md](https://github.com/user-attachments/files/32952320/README.md)[Uploading README.# 🔧 OTOPARCA DEPO PRO

Oto yedek parça işletmeleri için geliştirdiğim **depo ve stok yönetimi** uygulaması. Tezgâhtaki satıştan rafın sayımına kadar parçaların depodaki yolculuğunu takip eder. Parçayı OEM numarası ya da araç bilgisiyle saniyeler içinde bulur ve hangi rafta olduğunu gösterir.

Python standart kütüphanesi dışında hiçbir bağımlılığı yoktur. Kurulum gerektirmez, tek dosyadan çalışır.

![Parça arama](ekran_goruntuleri/parca_arama.png)

## Özellikler

| Özellik | Ne yapıyor |
| --- | --- |
| **Akıllı parça arama** | OEM numarasını yazım farkı gözetmeden bulur (`7701208265`, `7701 208 265` ve `77-01-208` aynı parçayı getirir). Parça adı, marka, raf veya araçla da arar (ör. "clio"). Araç markası, model ve yıl filtreleri vardır |
| **Araç uyumluluğu** | Her parça için `Marka \| Model \| 2012-2019` biçiminde uyumlu araç listesi tutulur |
| **Raf adresleme** | `KORİDOR-RAF-GÖZ` (ör. `A-03-2`) standardı otomatik uygulanır. Seçilen parçanın rafı büyük puntoyla gösterilir |
| **Hareket fişleri** | Mal kabul, satış/servis çıkışı (araç plakası ile) ve müşteri iadesi. Fişler otomatik numaralanır (`CF-2026-00001`), gerekçeyle iptal edilir |
| **Barkod desteği** | Fiş ekranında barkod okutup Enter'a basınca parça doğrudan eklenir |
| **Ağırlıklı ortalama maliyet** | Her mal kabulde parça maliyeti yeniden hesaplanır. Fiş iptal edilince eski maliyete döner |
| **Depo sayımı** | Koridor veya raf bazında sayım başlatılır, sayılan miktarlar girilir, fark ve tutarı görülür, tek tıkla stoğa uygulanır. Sayım sırasında yapılan satışlar fark hesabını bozmaz |
| **Raf etiketi** | Seçilen parçalar için **Code 128 barkodlu**, A4'e yazdırılabilir etiket sayfası. Barkod harici kütüphane olmadan üretilir |
| **Sipariş önerisi** | Kritik seviyedeki parçalar için önerilen sipariş miktarı ve son alındığı tedarikçi |
| **Raporlar** | Kategori bazında stok değeri, en çok çıkan parçalar, 90 gündür satılmayan (ölü) stok, sayım için raf listesi. Tümü CSV olarak dışa aktarılabilir |
| **CSV içe/dışa aktarma** | Toplu parça yükleme ve güncelleme. Hatalı satırlar atlanıp satır numarasıyla raporlanır |
| **Roller ve işlem kaydı** | Yönetici, Depo sorumlusu ve Tezgâh rolleri. Tüm kritik işlemler kullanıcı ve zaman bilgisiyle kaydedilir |

<p>
  <img src="ekran_goruntuleri/cikis_fisi.png" width="49%" alt="Çıkış fişi">
  <img src="ekran_goruntuleri/depo_sayimi.png" width="49%" alt="Depo sayımı">
</p>

![Raf etiketleri](ekran_goruntuleri/raf_etiketi.png)

## Teknik tasarım

- **Katmanlı yapı:** İş kuralları (`Depo` sınıfı) arayüzden ayrıdır ve arayüz açılmadan test edilir.
- **Veri bütünlüğü:**
  - Fişler, iptaller ve sayım uygulaması tek **transaction** içinde yapılır.
  - Stok hiçbir koşulda eksiye düşmez (uygulama kontrolü ve veritabanında `CHECK` kısıtı).
  - Aynı marka ve OEM numarasıyla iki kayıt açılamaz.
- **Para hesabı:** Tutarlar tamsayı kuruş olarak saklanır.
- **Güvenlik:** Tuzlu PBKDF2-SHA256 şifreleme, hatalı girişte hesap kilitleme, parametreli SQL sorguları ve her işlemde sunucu tarafı rol kontrolü.
- **Performans:** OEM, araç ve hareket tablolarında indeksler bulunur.

**Kullanılan teknolojiler:** Python 3.10+, tkinter/ttk, SQLite, unittest

## Çalıştırma

```bash
python otoparca_depo_pro.py            # uygulamayı açar
python otoparca_depo_pro.py --demo     # örnek parça ve fişlerle açar
python otoparca_depo_pro.py --test     # 14 birim testini çalıştırır
```

İlk girişte kullanıcı adı `admin`, şifre `admin123`'tür. Uygulama ilk girişte yeni şifre belirlemenizi ister.

**CSV biçimi** (`;` ayraçlı, UTF-8):

```
OEM;Marka;Parça adı;Kategori;Raf;Stok;Kritik;Alış;Satış;Uyumlu araçlar
HU 7008 z;MANN;Yağ filtresi;Filtre;B-02-1;5;2;190;420;Volkswagen | Golf VII | 2012-2020
```

**.exe olarak paketleme (Windows):**

```bash
pip install pyinstaller
pyinstaller --onefile --windowed --name "OTOPARCA DEPO PRO" otoparca_depo_pro.py
```

## Testler

Testler; OEM ve raf normalizasyonunu, tüm arama yollarını, araç/yıl filtresini, ağırlıklı maliyeti, stok yetersizliğinde geri almayı (rollback), fiş iptalinde maliyetin geri dönmesini, rol yetkilerini, sayım sırasında yapılan satışın fark hesabını bozmamasını, sipariş önerisini, CSV içe/dışa aktarmayı ve Code 128 barkod kodlamasını kapsar.

```
Ran 14 tests ... OK
```

## Geliştirici

**Enes Talha Köse** · [github.com/enestalhakose](https://github.com/enestalhakose)

Sakarya'da yaşıyorum ve kendimi yazılım alanında geliştirmek istiyorum. Karaca/Korkmaz yetkili servisinde çalışırken parça ve servis süreçlerini yakından gördüm. Bu uygulamayı, sahadaki gerçek sorunlara (parçayı doğru rafta bulmak, sayım farkları, kritik stok) çözüm üreten bir depo sistemi olarak tasarladım.
md…]()
