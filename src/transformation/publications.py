"""
============================================================
Transformasi data publikasi: raw -> staging -> core
============================================================

File ini berisi mapper PURE dari payload hasil scraping Google
Scholar (lihat src/ingestion/scrape_scholar_playwright.py) ke
nama kolom tabel-tabel core (lihat sql/ddl/02_create_core_tables.sql).

Murni (pure): tidak ada koneksi database, tidak ada permintaan
jaringan, tidak ada pembacaan/penulisan file. Satu-satunya
entrannya adalah dict `raw` dan hasilnya hanya dict biasa, sehingga
fungsi ini aman dipanggil berkali-kali dengan input yang sama dan
aman diuji tanpa database sama sekali.

File ini adalah SATU-SATUNYA tempat di proyek ini yang tahu nama
kolom core. Layer lain (loading, mart) hanya boleh diberi nilai,
tidak menyusun ulang daftar kolom.

Kontrak input (dari scraper, diasumsikan sudah final):

    raw = {
        "scholar_id": str,          # Google Scholar user ID
        "name":       str | None,   # nama author dari #gsc_prf_in
        "h_index":    int | None,   # dari tabel #gsc_rsb_st
        "rows": [                   # satu dict per publikasi
            {
                "title":     str | None,
                "authors":   str | None,   # "A B, C D, ..."
                "venue":     str | None,
                "extra":     str | None,   # gabungan volume/number/pages
                "citations": int,
                "year":      str | None,   # contoh: "2019"
            },
            ...
        ],
        "failures": [ ... ],
    }

Nama kunci per-baris DI BAWAH INI APA ADANYA nama field aslinya di
Scholar (belum dinormalisasi). to_core_rows() tidak boleh
mengasumsikan kunci lain apa pun di dalam `raw`.

Cara pakai:

    from src.transformation.publications import to_core_rows

    core = to_core_rows(envelope["data"], retrieved_at=envelope["finished_at"])

`retrieved_at` sengaja dibuat argumen opsional, bukan diambil
dari dalam envelope secara otomatis, supaya fungsi ini tetap murni:
jam pengambilan data itu milik file envelope / pemanggil, bukan
hasil transformasi.
"""

import re

# ------------------------------------------------------------
# KONSTANTA
# ------------------------------------------------------------

# Nilai untuk kolom source / index_name di kedua tabel metrics.
# source = identitas teknis sumber data, index_name = nama tampilan
# dari indeks sitasi. Keduanya sengaja dibedakan.
SOURCE = "google_scholar"
INDEX_NAME = "Google Scholar"

# Pola tahun yang dianggap bersih: tepat 4 digit ASCII ("2019").
# Dipaksa ASCII supaya digit non-ASCII seperti "٢٠١٩" (yang lolos
# str.isdigit()) tidak ikut diterima.
_TAHUN_POLA = re.compile(r"^[0-9]{4}$")

# Placeholder untuk publication_id / author_id. Nilai None berarti
# "belum ada", bukan "NULL karena datanya memang tidak ada".
_BELUM_ADA = None


# ============================================================
# HELPER
# ============================================================

def _rapikan_teks(nilai):
    """Rapatkan string: rapikan spasi berlebih lalu potong tepi.

    Dipakai untuk semua kolom VARCHAR/TEXT core karena hasil scrape
    Scholar kadang mengandung newline, tab, atau spasi ganda.
    String kosong (atau yang isinya cuma spasi) dinormalkan jadi
    None supaya tidak masuk ke core sebagai string kosong yang
    menyesatkan.
    """
    if nilai is None:
        return None
    teks = " ".join(str(nilai).split())
    return teks or None


def _normalisasi_tahun(nilai):
    """Ubah tahun Scholar menjadi string tanggal untuk kolom DATE.

    Kolom core.publications.publication_date bertipe DATE, sedangkan
    Scholar hanya memberi tahun dalam bentuk string bebas, misalnya
    "2019", "2019-2020", "in press", atau "2019 Jan".

    Aturan yang dipakai: kalau cleansinya tahun 4 digit, hasilnya
    "YYYY-01-01". Selebihnya None.

    KEHILANGAN PRESISI: tanggal sintetis 1 Januari bukan tanggal
    asli publikasi. Scholar tidak memberi bulan dan hari, jadi
    mapper memakai 1 Januari sebagai nilai pengganti yang
    deterministik supaya tidak ada tebakan yang berubah-ubah
    antar-run. Kalau nanti ditambah kolom publication_year sendiri,
    angka 2019 bisa diambil dari sini tanpa membaca ulang sumber.

    Format yang sebenarnya kaya akan bulan/hari (mis. "2019 Jan 15")
    sengaja TIDAK diurai dulu: itu keputusan yang lebih besar dari
    cakupan file ini, dan parser tanggal yang terlalu nekat bisa
    diam-diam mengarang tanggal kalau formatnya berubah.
    """
    tahun = _rapikan_teks(nilai)
    if tahun is None or not _TAHUN_POLA.match(tahun):
        return None
    return "{}-01-01".format(tahun)


def _pecah_penulis(nilai):
    """Pecah string penulis Scholar menjadi daftar nama berurutan.

    Scholar menaruh semua penulis dalam satu string yang dipisahkan
    koma, urutannya sama dengan urutan tampil (penulis pertama di
    depan):

        "Budi Santoso, Budi A. Rahman, ..." -> 3 nama

    Elemen kosong dibuang, dan "..." dibuang juga karena Scholar
    memakai elipsis untuk menandai daftar yang dipotong. Aturan ini
    sengaja konservatif: nama tidak diubah bentuknya, urutan tidak
    dibalik, dan tidak digabung kembali menjadi satu string, supaya
    pemanggil tetap bisa membedakan penulis yang berbeda.
    """
    teks = _rapikan_teks(nilai)
    if teks is None:
        return []
    nama = [bagian.strip() for bagian in teks.split(",")]
    return [n for n in nama if n and set(n) != {"."}]


# ============================================================
# MAPPER UTAMA
# ============================================================

def to_core_rows(raw, retrieved_at=None):
    """Petakan payload scrape Google Scholar ke baris tabel core.

    Parameter
    ---------
    raw : dict
        Bagian `data` dari run envelope hasil scraper. Bentuknya
        lihat docstring modul.
    retrieved_at : str | None, opsional (keyword)
        Timestamp pengambilan data format ISO-8601, biasanya
        `envelope["finished_at"]`. Nilai ini diteruskan apa adanya
        ke kolom retrieved_at pada kedua tabel metrics; mapper tidak
        memformat ulang dan tidak memvalidasi, serta TIDAK PERNAH
        mengambil jam dari dalam dirinya sendiri (harus tetap murni).
        String kosong dinormalkan jadi None.

    Mengembalikan
    --------------
    dict dengan 5 kunci, dikelompokkan per tabel core:

        {
          "authors":             dict    (satu dict, bukan list)
          "publications":        list    (satu dict per baris `rows`)
          "publication_authors": list    (satu dict per penulis)
          "author_metrics":      dict    (satu dict, bukan list)
          "publication_metrics": list    (satu dict per baris `rows`)
        }

    Kunci di dalam tiap dict adalah nama kolom di DDL core, dengan
    nilai None untuk kolom yang belum bisa diisi pada tahap ini
    (lihat blok "CELAH YANG SENGAJA TIDAK DIPETAHKAN" di akhir
    file). Pengecualiannya dua kunci di publication_authors:
    `publication_index` dan `author_name`. Keduanya bukan kolom DB,
    melainkan kunci handoff yang sengaja dikeluarkan supaya
    pemanggil bisa menyambungkan baris ke publikasi yang tepat dan
    bisa mencari atau membuat author dari nama yang ada.

    Catatan: kolom SERIAL (authors.author_id, publications.publications_id,
    metric_id) tetap dikeluarkan sebagai kunci bernilai None, sama
    seperti kolom core lain, supaya bentuk hasil mapper seragam di
    kelima tabel. Nilainya memang baru ada setelah database memberi
    nomor urut.
    """
    raw = raw or {}
    diambil = _rapikan_teks(retrieved_at)
    baris = raw.get("rows") or []

    # ---------- core.authors ----------
    # Kolom name NOT NULL, jadi name=None berarti profil tidak
    # terbaca dan baris ini tidak layak masuk core apa pun. Mapper
    # tetap mengembalikan dict apa adanya (nilai None) supaya
    # pemanggil yang memutuskan skip atau quarantine, bukan mapper
    # yang diam-diam membuat data palsu.
    authors = {
        "author_id": _BELUM_ADA,       # SERIAL, dibuat database
        "name": _rapikan_teks(raw.get("name")),
        "sinta_id": None,             # tidak ada di sumber Scholar
        "scholar_id": _rapikan_teks(raw.get("scholar_id")),
        "scopus_id": None,            # tidak ada di sumber Scholar
        "program_study": None,        # tidak ada di sumber Scholar
        "faculty": None,              # tidak ada di sumber Scholar
    }

    # ---------- core.publications & core.publication_metrics ----------
    publications = []
    publication_metrics = []
    publication_authors = []

    for indeks, baris_pub in enumerate(baris):
        # Satu-ke-satu dengan baris `rows`: publications[i] dipasangkan
        # dengan publication_metrics[i] dan publication_index = i.
        # Urutan BARU TIDAK diubah, sehingga pemanggil tidak perlu
        # mencari publications_id berdasarkan isi.
        publications.append({
            "publications_id": _BELUM_ADA,   # SERIAL, diisi database
            "title": _rapikan_teks(baris_pub.get("title")),
            "publication_date": _normalisasi_tahun(baris_pub.get("year")),
            # fidelity RENDAH: `venue` dari Scholar adalah teks bebas
            # ("Journal of X, vol. 3, 2019, IEEE"), bukan nama jurnal
            # yang bersih. Dipetakan 1:1 apa adanya supaya tidak ada
            # data yang hilang, tapi jangan dianggap sudah rapi.
            "journal": _rapikan_teks(baris_pub.get("venue")),
            "volume": None,           # ada di row["extra"], lihat akhir file
            "pages": None,            # ada di row["extra"], lihat akhir file
            "publisher": None,        # tidak ada di sumber Scholar
            "description": None,      # tidak ada di sumber Scholar
            "category": None,         # tidak ada di sumber Scholar
            "doi": None,              # tidak ada di sumber Scholar
        })

        # citation_count: `citations` selalu int dari scraper (0 kalau
        # tidak ada angka sitasi), jadi tidak perlu dibungkus None.
        publication_metrics.append({
            "metric_id": _BELUM_ADA,        # SERIAL, dibuat database
            "publication_id": _BELUM_ADA,   # FK, belum ada sebelum load
            "source": SOURCE,
            "citation_count": baris_pub.get("citations"),
            "index_name": INDEX_NAME,
            # score: Scholar tidak punya skor numerik per publikasi,
            # yang ada hanya jumlah sitasi. None berarti "tidak ada di
            # sumber", bukan 0 - jangan diisi 0 karena itu akan
            # terbaca sebagai skor nol di mart.
            "score": None,
            "retrieved_at": diambil,
        })

        # ---------- core.publication_authors ----------
        # Yang bisa diisi di tahap ini HANYA (nama, urutan).
        # publication_id dan author_id adalah SERIAL/FK yang belum
        # ada sebelum load, jadi keduanya tidak bisa diisi di sini.
        #
        # author_order memakai indeks 0-based yang sama dengan
        # publication_index, jadi urutan penulis dan urutan publikasi
        # konsisten di seluruh hasil mapper.
        for urutan, nama in enumerate(_pecah_penulis(baris_pub.get("authors"))):
            publication_authors.append({
                # publication_index: BUKAN kolom DB, tapi kunci
                # handoff ke pemanggil. Penanda posisi (0-based) di
                # list hasil["publications"], dipakai untuk
                # menyambungkan baris ini ke baris publikasi yang
                # tepat setelah publications_id diberikan database.
                "publication_index": indeks,
                "publication_id": _BELUM_ADA,   # FK, diisi setelah load
                "author_id": _BELUM_ADA,        # FK, diisi setelah load
                # author_name: BUKAN kolom DB, tapi kunci handoff ke
                # pemanggil. Dipakai sebagai kunci pencarian author di
                # core.authors, dan kalau tidak ditemukan, sebagai
                # nama untuk INSERT author baru. Sudah disepakati
                # bahwa nama ini tidak perlu disimpan lagi di core
                # karena aslinya sudah ada di lapisan raw.
                "author_name": nama,
                "author_order": urutan,
            })

    # ---------- core.author_metrics ----------
    # author_metrics tetap satu dict walaupun raw["h_index"] None:
    # h_index boleh NULL di kolom, jadi baris metriknya tetap sah.
    author_metrics = {
        "metric_id": _BELUM_ADA,       # SERIAL, dibuat database
        "author_id": _BELUM_ADA,       # FK, diisi setelah load
        "source": SOURCE,
        "h_index": raw.get("h_index"),
        "score": None,                 # Scholar tidak punya skor numerik
        "index_name": INDEX_NAME,
        "retrieved_at": diambil,
    }

    return {
        "authors": authors,
        "publications": publications,
        "publication_authors": publication_authors,
        "author_metrics": author_metrics,
        "publication_metrics": publication_metrics,
    }


# ============================================================
# CATATAN UNTUK PEMANGGIL (loading/postgres.py)
# ============================================================
#
# 1) Urutan penyambungan
#    publications dan publication_metrics dijaga sejajar: elemen
#    ke-i dari keduanya berasal dari raw["rows"][i].
#    publication_authors memakai publication_index untuk menunjuk
#    elemen yang sama.
#
# 2) publication_authors: resolusi nama dipegang pemanggil
#    author_id harus hasil resolusi author_name ke
#    core.authors.author_id. author_name dan publication_index BUKAN
#    kolom DB, tapi keduanya memang sengaja dihasilkan sebagai kunci
#    handoff - bukan sisa yang menggantung.
#
#    Pemanggil diharapkan melakukan, minimal:
#      a) INSERT publications, lalu catat publications_id yang
#         dikembalikan per indeks. Karena urutan INSERT sama dengan
#         urutan list, publications_id untuk indeks i = hasil ke-i.
#      b) Untuk tiap baris publication_authors, cari author_name di
#         core.authors dengan pencocokan PERSIS, TIDAK membedakan
#         huruf besar-kecil.
#           - ditemukan    -> pakai author_id yang sudah ada.
#           - tidak ditemukan -> INSERT author baru yang HANYA kolom
#             name terisi, lalu pakai author_id hasil INSERT itu.
#      c) INSERT publication_authors dengan publication_id dari (a)
#         dan author_id dari (b). author_order dipakai apa adanya,
#         tidak diurutkan ulang.
#
#    OTORITA AUTO-CREATE AUTHOR
#    Membuat author baru hanya mengisi kolom name. Kolom sinta_id,
#    scopus_id, program_study, dan faculty TIDAK diisi - bukan karena
#    lupa, tapi karena tidak ada sumbernya dan DDL sengaja tidak
#    diubah untuk accommodate apa pun. Baris author yang dibuat lewat
#    jalur ini akan terlihat minim isinya di core; itu yang disepakati.
#
#    Nama mentah dari Scholar dianggap sudah tersimpan di lapisan raw,
#    sehingga tidak wajib dipertahankan lagi di core setelah
#    author_id terbentuk. Karena itu tidak ada kolom baru yang
#    diperlukan untuk menampungnya di sini.
#
#    CATATAN PENTING: uraian di atas adalah KONTRAK yang sudah
#    diputuskan, bukan fakta bahwa loader sudah jalan. Di akhir file
#    ini ditulis bahwa loading/postgres.py masih berupa TODO; belum
#    ada implementasi maupun pengujian atas kebijakan ini. Yang
#    dijamin mapper hanya isi publication_authors, bukan hasil
#    resolusinya.
#
# 3) authors.author_id juga belum ada
#    author_metrics.author_id diisi setelah authors di-INSERT; kedua
#    dict itu menggambarkan satu author yang sama, bukan dua baris
#    yang berbeda.
#
# 4) retrieved_at
#    Kalau argumen retrieved_at tidak diberi, kedua tabel metrics
#    mendapat NULL. Itu sah secara skema, tapi metrik tanpa jam
#    pengambilan sulit diaudit, jadi sebaiknya selalu diteruskan
#    envelope["finished_at"].
#
#
# ============================================================
# KEBIJAKAN PENCOCOKAN AUTHOR (sudah diputuskan, sementara)
# ============================================================
#
# Letak kebijakan ini adalah PEMANGGIL (loading/postgres.py), bukan
# mapper ini. Mapper tidak pernah mencari maupun membuat author; dia
# hanya mengeluarkan author_name apa adanya. Dicatat di sini supaya
# pembaca file ini tahu keputusannya sudah diambil.
#
# - Pencocokan: nama PERSIS, TIDAK membedakan huruf besar-kecil.
#   Sederhana dengan sengaja, supaya tidak butuh data tambahan apa
#   pun untuk berjalan.
# - Auto-create: nama yang tidak ditemukan memicu INSERT author baru
#   dengan kolom name saja (lihat blok CATATAN di atas).
#
# Risiko duplikat author DITERIMA untuk sekarang, bukan diabaikan.
# Dua hal penyebabnya, dan keduanya adalah alasan penyempurnaan
# terhadap daftar faculty resmi nanti hari:
#
#   a) DDL tidak punya batasan UNIQUE pada core.authors.name. Dua
#      proses yang jalan bersamaan (mis. dua run ETL tumpang tindih)
#      bisa sama-sama membuat author dengan nama yang sama sebelum
#      salah satunya terlihat. Tidak ada yang menahan ini di level
#      database.
#   b) Pencocokan nama tidak membedakan orang. Dua orang sungguhan
#      dengan nama identik akan dianggap satu author yang sama.
#
# Penyempurnaan yang direncanakan: mencocokkan terhadap daftar
# faculty resmi (sinta_id, program_study, fakultas) alih-alih nama
# teks bebas. Itu butuh sumber master yang belum tersedia, dan DDL
# juga belum punya kolom untuk menampungnya.
#
#
# ============================================================
# CELAH YANG SENGAJA TIDAK DIPETAHKAN (known gaps)
# ============================================================
#
# Semua hal di bawah ini ADA di sumber data atau di DDL, tapi tidak
# bisa diisi oleh mapper ini. Dicatat di sini supaya tidak hilang
# dan supaya mapper tidak diam-diam mengarang nilai.
#
# - row["extra"]
#     Berisi volume/number/pages dalam SATU string Scholar
#     ("3(2), 45-60"). Tidak dipecah otomatis: memisahkannya butuh
#     parser per pola jurnal, dan pola yang salah akan menulis
#     volume/halaman yang salah ke core. Keputusan: biarkan dulu
#     sampai ada sumber yang bisa diurai bersih atau ada keputusan
#     eksplisit soal aturannya.
#
# - publications.publisher / description / category / doi
#     Tidak ada di profil Scholar sama sekali. Butuh sumber lain
#     (mis. Crossref / SINTA) atau scraping tambahan.
#
# - authors.sinta_id / scopus_id / program_study / faculty
#     Semuanya atribut institutional, bukan atribut indeks sitasi.
#     Google Scholar tidak memuatnya. Harus berasal dari sistem
#     internal. Berkaitan dengan auto-create author: kolom-kolom ini
#     juga TIDAK diisi pada author yang dibuat otomatis, dan itu
#     disengaja.
#
# - authors.name ketika None
#     Kolomnya NOT NULL, jadi mapper mengembalikan None dan
#     menyerahkan keputusan pada pemanggil. Menebak nama dari
#     publication_authors akan menghasilkan author palsu.
#
# - publication_authors.author_name dan publication_index
#     SUDAH SELESAI, bukan gap lagi. Keduanya tetap dipetakan walau
#     bukan kolom DB karena memang sengaja dihasilkan sebagai kunci
#     handoff ke pemanggil: author_name untuk mencari author, atau
#     menjadi nama saat INSERT author baru; publication_index untuk
#     menyambungkan baris itu ke publikasi yang tepat. Nama tidak
#     perlu disimpan lagi di core karena aslinya sudah ada di lapisan
#     raw. Lihat blok CATATAN dan KEBIJAKAN di atas.
#
# - metric_id, authors.author_id, publications.publications_id,
#   publication_authors.publication_id, publication_authors.author_id,
#   author_metrics.author_id, publication_metrics.publication_id
#     Semuanya SERIAL/FK. Tidak mungkin ada sebelum INSERT, jadi
#     dibiarkan None untuk diisi database atau pemanggil.
