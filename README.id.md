# Faktura

[English](README.md)

Otomasi Accounts Payable: membaca faktur, mencocokkannya dengan PO dan bukti terima barang
(*three-way match*), lalu hanya meneruskan faktur bermasalah ke manusia.

> **Status: Fase 1 (MVP) selesai.** Sudah ada data sintetis berlabel, ekstraksi dengan
> baca ulang terarah, validator deterministik, mesin pencocokan dengan eval end-to-end,
> benchmark CORD, API antrean review dengan maker-checker dan audit trail tahan
> manipulasi, serta UI. Fase 2 berjalan: data master dibaca langsung dari ERP (Odoo 20),
> dan faktur yang disetujui diposting balik sebagai vendor bill, serta login lewat
> Keycloak. Belum ada: deployment.
> README ini hanya menjelaskan yang sudah ada di kode.

## Dataset sintetis

```bash
uv sync
uv run faktura-synth --config configs/synth.yaml --out data/synth/v1
uv run pytest
```

Generator membuat vendor, PO, bukti terima barang, dan faktur PDF. Faktur PDF dibuat dengan
variasi bahasa (ID/EN), format tanggal, format NPWP, dan efek hasil scan. Sebagian faktur
sengaja diberi satu anomali:

| Anomali | Contoh |
|---|---|
| `price_mismatch` | Harga satuan di faktur lebih tinggi 2–15% dari PO |
| `qty_over_received` | Jumlah ditagih melebihi barang yang diterima |
| `duplicate_invoice` | Faktur yang sama dikirim ulang |
| `npwp_mismatch` | NPWP berbeda dari master vendor |
| `vat_miscalc` | PPN dihitung dengan tarif yang salah |
| `bank_account_changed` | Rekening tujuan berbeda dari master vendor |

### v1 dan v2

v1 (`configs/synth.yaml`) memakai satu layout dengan scan ringan. Uji kecil 10 faktur v1
dengan Luna mendapat akurasi 100%. Ini tanda datanya terlalu mudah, bukan tanda modelnya
sempurna. v2 (`configs/synth_v2.yaml`) sengaja dibuat lebih sulit. Setiap faktor kesulitan
dicatat terpisah, supaya eval bisa menunjukkan faktor mana yang membuat ekstraksi gagal:

| Faktor | Variasi |
|---|---|
| Layout | classic, modern (header berwarna, baris zebra), compact (font kecil, nomor faktur pajak di footer), borderless (tanpa garis, total di kiri) |
| Format angka | `Rp 1.234.567`, `Rp1.234.567,00`, `IDR 1,234,567` |
| Format tanggal | `05/03/2025`, `5 Maret 2025`, `2025-03-05`, `05-Mar-2025` |
| Deskripsi | 30% item memakai deskripsi panjang yang terpotong ke dua baris |
| Degradasi | 0 PDF bersih, 1 scan ringan, 2 scan jelek (lipatan, kontras rendah, JPEG), 3 foto HP (perspektif, pencahayaan tidak rata) |
| Stempel | 40% faktur level 2–3 diberi stempel "DITERIMA" yang menimpa area total |

Sebagian faktur level 3 yang terkena stempel memang memiliki digit yang tidak terbaca,
bahkan oleh manusia. Pada kasus itu yang diukur adalah apakah model menandai field
tersebut sebagai ragu, bukan apakah tebakannya kebetulan benar.

Karena anomalinya disisipkan sendiri, label benarnya didapat tanpa labeling manual. Test
`test_labels_match_independent_oracle` memeriksa ulang setiap label dengan aturan yang
ditulis terpisah dari generator.

## Ekstraksi faktur

Halaman PDF dirender ke gambar (~170 DPI), lalu dibaca model vision lewat Azure AI Foundry
Responses API dengan output terstruktur (Pydantic). Model hanya menyalin apa yang tercetak.
Perhitungan ulang dan keputusan dikerjakan kode deterministik di tahap pencocokan.

Model tidak diminta memberi skor confidence berupa angka, karena angka yang dilaporkan model
sendiri biasanya tidak terkalibrasi. Sebagai gantinya, model menandai field yang sulit dibaca
(`uncertain_fields`), dan eval mengukur seberapa banyak kesalahan yang benar-benar tertandai.

```bash
cp .env.example .env   # isi endpoint dan key Azure pribadi
uv run faktura-eval-extract --run runs/luna-smoke --limit 10
```

Laporan eval berisi akurasi per field, persentase faktur yang benar sempurna, hasil per
kondisi (hasil scan atau bukan, ID atau EN), kualitas penanda ketidakpastian, latensi
p50/p95, dan estimasi biaya per 1.000 faktur. Output mentah model disimpan per faktur, jadi
run yang terputus bisa dilanjutkan tanpa membayar dua kali.

## Keputusan pembayaran (three-way match)

Setiap faktur mendapat satu dari empat keputusan, dan setiap keputusan membawa alasan
yang bisa dibaca auditor:

| Keputusan | Artinya |
|---|---|
| `approve` | Cocok dengan PO, bukti terima barang, dan master vendor, jadi dibayar otomatis |
| `review_reading` | Hasil baca diragukan, jadi manusia mencocokkannya dengan dokumen |
| `business_exception` | Harga, jumlah, atau PPN menyimpang dari PO, jadi perlu persetujuan bisnis |
| `fraud_hold` | Rekening atau NPWP berubah, atau faktur duplikat, jadi pembayaran ditahan |

Rekening atau NPWP yang beda dari master **selalu ditahan**, walaupun kemungkinan hanya
salah baca. Perubahan data pembayaran adalah modus fraud AP yang paling umum.

Hasil pada 340 faktur sintetis v2, memakai pembacaan model sungguhan (bacaan pertama
ditambah baca ulang field yang ditandai):

- **Anomali tertangkap: 99 dari 99.** Uang yang dipertaruhkan Rp775,6 juta, dan 100%
  tertangkap.
- **Faktur bersih yang dibayar dengan nominal atau rekening salah baca: 0.**
- **Faktur bersih yang lolos otomatis:** 100% untuk PDF, 98% untuk scan ringan, 64% untuk
  scan jelek, dan 17% untuk foto HP. Sisanya dikirim ke manusia.

```bash
uv run faktura-eval-match --oracle            # menguji aturan dengan pembacaan sempurna
uv run faktura-eval-match --runs runs/<run>   # menguji dengan pembacaan model
```

## Antrean review dan audit trail

Faktur yang tidak lolos otomatis masuk antrean. Aturan alur kerjanya dijaga di API, bukan
di UI:

- **Maker-checker.** Staf AP (maker) mengoreksi atau mengonfirmasi field, lalu mesin
  pencocokan memutuskan ulang. Faktur yang pernah disentuh manusia harus disetujui
  approver lain. Orang yang mengubah faktur tidak bisa menyetujuinya.
- **`fraud_hold`** hanya bisa diajukan setelah verifikasi vendor dicatat (siapa yang
  dihubungi, lewat kanal apa, dan apa yang dikonfirmasi).
- **Hasil baca asli model tidak pernah ditimpa.** Koreksi manusia disimpan terpisah.
- **Audit trail tahan manipulasi.** Setiap event menyimpan hash event sebelumnya, sehingga
  `GET /audit/verify` mendeteksi baris yang diubah atau dihapus. Di Postgres, trigger
  menolak UPDATE dan DELETE pada tabel audit.
- **Optimistic concurrency.** Aksi dengan versi faktur yang sudah usang ditolak (409).

```bash
docker compose up -d db
export DATABASE_URL=postgresql+psycopg://faktura:faktura-dev@127.0.0.1:5433/faktura
uv run alembic upgrade head
uv run faktura-seed --runs runs/luna-v2-reread-tiles-final runs/luna-v2-reread-pilot-tiles runs/luna-v2
docker compose --profile sso up -d keycloak
uv run uvicorn faktura.app.api:app --port 8077   # Swagger UI di /docs
```

**Login (Keycloak, OIDC).** Setiap panggilan API membawa access token Keycloak. API
memeriksa tanda tangan, issuer, audience, dan masa berlakunya, lalu mengambil username dan
role dari token itu. Role realm: `ap_staff` dan `approver`; akun yang memegang keduanya
ditolak. Detail, tabel ancaman, dan akun demo ada di [`docs/auth.md`](docs/auth.md).

### UI antrean review

![Antrean review](docs/screenshots/1-queue.png)

Halaman detail menampilkan gambar faktur di samping hasil baca. Field yang ditandai
disorot, dan nilai asli bacaan model tetap terlihat di samping koreksi manusia:

![Faktur ditahan karena NPWP salah baca](docs/screenshots/2-held-for-npwp.png)

Contoh alur di screenshot: NPWP terbaca dengan dua digit tertukar, sehingga faktur
ditahan. Staf AP (Budi) mengoreksinya sesuai dokumen, dan mesin pencocokan memutuskan
ulang. Karena faktur sudah disentuh manusia, pembayaran tetap perlu disetujui approver
lain (Sari). Seluruh langkah tercatat di audit trail:

![Disetujui, dengan audit trail](docs/screenshots/4-approved-with-audit.png)

```bash
cd web && npm install && cp .env.example .env.local   # isi FAKTURA_SESSION_SECRET
npm run build && npm start                            # http://127.0.0.1:3077
npm run verify                                        # typecheck + lint + unit test
```

Next.js 16 memanggil FastAPI dari server, jadi browser tidak pernah berbicara langsung
ke API dan tidak pernah memegang token. Design token-nya sama dengan TokenWatch.

**Test end-to-end di browser sungguhan** (Playwright) menjalankan skenario di atas dan
menghasilkan screenshot-nya. Setiap user login lewat form Keycloak:

```bash
uv run playwright install chromium
uv run python scripts/e2e_demo.py --out docs/screenshots   # Keycloak, API, dan web harus sudah jalan
```

## Belajar dari koreksi

Setiap koreksi dari staf AP adalah contoh berlabel tentang cara model salah membaca.
Dulu NPWP dan nomor rekening yang salah baca ikut masuk antrean fraud (39 dari 241 faktur
bersih). Dari koreksi terlihat bahwa salah bacanya sistematis: `6` terbaca `0`, `9`
terbaca `0`, `6` terbaca `8`, atau satu digit terbaca dua kali. `faktura-learn` mengubah
koreksi di audit trail menjadi tabel pola salah baca yang berversi dan bisa diaudit.
Selisih dengan data master yang persis menyerupai pola itu ditriase sebagai
`probable_misread`: staf mencocokkan digitnya dengan halaman lebih dulu, dan nilai yang
memang tercetak berbeda tetap ditahan seperti sebelumnya. Model ini tidak pernah
menyetujui apa pun, dan faktur tidak bisa di-submit sebelum pengecekan itu.

Diuji ulang sesuai urutan kedatangan, dengan model hanya belajar dari koreksi
sebelumnya: alarm fraud palsu turun dari 39 menjadi 20, presisi naik dari 0,57 ke 0,71,
ke-25 perubahan NPWP/rekening asli tetap ditahan, dan 235 pemalsuan satu digit tertahan
semua setelah pengecekan halaman. Memasukkan pola yang sama ke prompt re-read justru
**menurunkan** akurasi (73,0% → 66,9% pada data held-out), jadi tidak diadopsi. Detail:
[`docs/learning.md`](docs/learning.md).

## Integrasi ERP (Odoo 20)

Faktura berkomunikasi dengan Odoo lewat API JSON-2, memakai user integrasi khusus.

- **Membaca data master.** Vendor, PO, dan bukti terima barang diambil langsung dari Odoo.
  Mesin pencocokan tidak diubah. Setelah Odoo diisi dari file sintetis, ke-340 faktur
  mendapat keputusan, status, dan alasan yang sama persis dengan mode file. Hanya rekening
  yang ditandai *trusted* di Odoo yang dihitung, jadi rekening baru yang ditambahkan di ERP
  tetap ditahan sampai diverifikasi oleh orang yang berwenang.
- **Memposting faktur yang disetujui sebagai vendor bill.** Persetujuan dan entri outbox
  ditulis dalam satu transaksi. Worker lalu memposting tagihan yang tertaut ke baris PO,
  lengkap dengan PDF-nya. Setiap langkah aman diulang, jadi crash atau respons yang hilang
  tidak pernah membuat tagihan ganda. Pada seluruh dataset: 165 faktur yang disetujui
  menjadi 165 tagihan di Odoo, dengan total yang sama sampai ke rupiah (Rp2.262.040.029).

```bash
docker compose --profile erp up -d          # setelah setup awal di docs/erp.md
FAKTURA_MASTER=odoo uv run uvicorn faktura.app.api:app --port 8077
uv run faktura-erp-sync                     # memposting faktur yang disetujui ke Odoo
```

Cara setup, pemetaan field, cara posting dibuat idempoten, dan padanannya di SAP S/4HANA
ada di [`docs/erp.md`](docs/erp.md).

## Benchmark eksternal: CORD v2

Untuk perbandingan dengan model publik, ekstraksi juga diuji di
[CORD v2](https://huggingface.co/datasets/naver-clova-ix/cord-v2) (struk toko dan restoran
Indonesia, CC BY 4.0). Penilaiannya memakai kode metrik resmi Donut, yang diporting
lengkap dengan lisensi MIT-nya, sehingga angkanya bisa dibandingkan langsung.

| Model | Latihan di CORD | Akurasi TED | F1 |
|---|---|---|---|
| gpt-6-luna (zero-shot, prompt p2) | tidak ada | **91,9** | 82,1 |
| Donut (fine-tuned) | 800 struk | 91,3 | **84,1** |

Prompt disetel hanya di split validasi, lalu split uji dijalankan satu kali. Selisih F1
sebagian besar berasal dari format salinan, misalnya `8,000` ditulis `8.000` pada struk
dot-matrix. Kalau perbedaan spasi, huruf besar-kecil, dan pemisah ribuan diabaikan, F1
menjadi 86,9. Angka ini metrik sekunder dan tidak bisa dibandingkan dengan angka publikasi.

Catatan: perbandingan zero-shot dengan model fine-tuned tidak setara, dan CORD sudah publik
sejak 2019 sehingga mungkin termasuk data pra-latih model. Detail lengkap ada di
[`docs/experiments.md`](docs/experiments.md).

```bash
uv run faktura-bench-cord --split validation --prompt p2 --run runs/cord-val-p2
```

Semua data fiktif, dan setiap halaman bertanda "CONTOH SINTETIS". Asumsi domain dicatat
di [`docs/assumptions.md`](docs/assumptions.md).

## Lisensi

[MIT](LICENSE).

src/faktura/bench/donut_metric.py is adapted from [Donut](https://github.com/clovaai/donut) (Copyright (c) 2022-present NAVER Corp., MIT); its notice is kept in that file.
