"""
============================================================
Author record per publikasi: helper PURE untuk
raw.scholar_publication_author (Tahap 1, P57)
============================================================

File ini menyusun SATU record dict per penulis per publikasi untuk
tabel `raw.scholar_publication_author` yang desainnya dibekukan di
keputusan_desain_author_p56.txt. Murni (pure): tidak ada koneksi
database, tidak ada permintaan jaringan, tidak ada pembacaan atau
penulisan file. Hasilnya hanya dict biasa, jadi aman dipanggil
berkali-kali dengan input yang sama dan aman diuji tanpa database.

File ini TIDAK mengisi tabel apa pun - pemanggilan ke loader
dilakukan di Tahap 2 (di luar cakupan sekarang). Di sini record
dikembalikan sebagai list dict biasa.

Kontrak input
-------------
    1. Envelope hasil scrape:
         raw["name"] = nama pemilik profil dari #gsc_prf_in.
       scholar_id run ini DIPEROLEH DARI PEMANGGIL (argumen CLI /
       author_id daftar target), BUKAN ditebak dari isi data.
       Menebak dari envelope = menulis identitas yang tidak tentu
       benar ke kolom NOT NULL (alasan yang sama dengan keputusan
       `scholar_id` wajib di src/loading/postgres.py).

    2. Baris referensi Excel SUDAH DIMUATKAN PEMANGGIL berupa list
       dict dengan kunci `no`, `nama`, `scholar_id` - bentuk
       persis keluaran `baca_daftar` di
       src/pipeline/run_batch.py (lihat baca_daftar). MEMBACA file
       Excel itu sendiri BUKAN tanggung jawab modul ini; wiring
       `baca_daftar` terjadi di Tahap 2.

Catatan tepi yang DIKETAHUI (didokumentasikan, bukan diperbaiki):
    _pecah_penulis membuang bagian yang isinya "..." tapi
    membiarkan elipsis Unicode "…" tunggal lolos sebagai nama.
    Data Scholar nyata memakai "...", dan
    deteksi_daftar_terpotong mengenali KEDUA ejaan itu.

Cara pakai:

    from src.loading.author_records import bangun_record_author

    records = bangun_record_author(
        baris["detail_authors"] or baris["authors"],
        sumber="detail" if baris.get("detail_authors") else "daftar",
        nama_profil=raw["name"],
        baris_referensi=baris_referensi,
        scholar_id_run=scholar_id,
    )
"""

from src.loading.postgres import (
    _bentuk_pendek,
    _kunci_nama,
    _varian_nama_pemilik,
)
from src.transformation.publications import _pecah_penulis

# ------------------------------------------------------------
# KONSTANTA
# ------------------------------------------------------------

# author_role: hanya fungsi dari urutan tampil (K5). Tidak bergantung
# pada verifikasi identitas apa pun.
ROLE_PERTAMA = "penulis_pertama"
ROLE_CO = "co_author"

# profile_owner_status (K6): dimensi INDEPENDEN dari author_role.
# BELUM dipakai dua makna sekaligus: nilai status verifikasi identitas
# ("belum_terverifikasi") dan nilai kolom untuk record yang tidak
# terverifikasi - keduanya memang teks yang sama.
PEMILIK = "pemilik_profil"
BUKAN = "bukan_pemilik_profil"
BELUM = "belum_terverifikasi"

# Nilai `sumber` (D-1): "detail" = daftar penulis halaman detail
# (lengkap), "daftar" = kolom authors halaman profil (bisa terpotong).
SUMBER_DETAIL = "detail"
SUMBER_DAFTAR = "daftar"

# Status verifikasi identitas pemilik profil. Beda dari PEMILIK:
# ini hasil GERBANG verifikasi Excel, bukan penanda per record.
_STATUS_OK = "terverifikasi"


# ============================================================
# HELPER
# ============================================================

def peran_dari_urutan(urutan):
    """Tentukan author_role HANYA dari urutan penulis (K5).

    Penulis pada urutan 0 = penulis_pertama, sisanya co_author.
    Sengaja tidak melihat verifikasi identitas sama sekali: peran
    dan kepemilikan profil adalah dua dimensi berbeda yang disimpan
    di kolom terpisah (K7), jadi penulis pertama belum tentu
    pemilik profil dan pemilik profil belum tentu penulis pertama
    (K9).
    """
    if urutan == 0:
        return ROLE_PERTAMA
    return ROLE_CO


def deteksi_daftar_terpotong(teks_daftar):
    """True kalau daftar penulis dipotong elipsis oleh Scholar.

    Scholar menulis "A B, C D, ..." pada kolom authors halaman
    profil kalau daftar penulisnya terlalu panjang. Tanpa penanda
    ini (D-1), query tidak bisa membedakan "daftar memang segini"
    dari "daftar dipotong", dan K12 melarang menyimpulkan kelengkapan
    dari kegagalan. Yang dicek hanya token elipsis per bagian koma
    ("..." ejaan data nyata, atau "…" Unicode); nama biasa yang
    mengandung titik ("Ph.D.") bukan elipsis.

    None/kosong -> False; tidak pernah melempar.
    """
    if not teks_daftar:
        return False
    for bagian in str(teks_daftar).split(","):
        if bagian.strip() in ("...", "\u2026"):
            return True
    return False


def verifikasi_pemilik_profil(nama_profil, scholar_id_run, baris_referensi):
    """Gerbang verifikasi identitas pemilik profil terhadap Excel.

    Mengembalikan tuple (status, alasan):

        status = "terverifikasi" | "belum_terverifikasi"
        alasan = "referensi_tidak_ada" | "id_run_kosong"
               | "nama_profil_kosong"  | "id_tidak_ditemukan"
               | "nama_kembar_beda_id" | "nama_tidak_cocok" | "ok"

    Urutan pemeriksaan penting dan sengaja konservatif (K8):
    referensi kosong -> identitas gagal total; id/nama kosong ->
    input run belum lengkap; id tidak ada di Excel -> nama profil
    tidak punya jangkar identitas; id yang sama ternyata dipakai
    oleh dua nama BERBEDA -> ambigu, TIDAK boleh ditebak mana yang
    benar; baru kemudian pencocokan nama.

    Pencocokan nama TERPAKSA eksak setelah _kunci_nama (spasi
    dirapatkan + casefold) - TANPA fuzzy matching. Kemiripan nama
    saja tidak pernah cukup untuk menetapkan kepemilikan (K8):
    salah tebak = satu orang menandai publikasi orang lain sebagai
    miliknya.

    Tidak pernah melempar untuk data yang buruk (None, string
    kosong, list rujukan tidak cocok) - semuanya cukup gagal
    verifikasi dengan alasan.
    """
    if not baris_referensi:
        return (BELUM, "referensi_tidak_ada")

    id_bersih = None if scholar_id_run is None else str(scholar_id_run).strip()
    if not id_bersih:
        return (BELUM, "id_run_kosong")

    if nama_profil is None or not str(nama_profil).strip():
        return (BELUM, "nama_profil_kosong")

    cocok_id = []
    for r in baris_referensi:
        baris = r if isinstance(r, dict) else {}
        if str(baris.get("scholar_id") or "").strip() == id_bersih:
            cocok_id.append(baris)
    if not cocok_id:
        return (BELUM, "id_tidak_ditemukan")

    # id sama boleh muncul berulang KALAU namanya juga sama (baris
    # duplikat di Excel itu biasa). id sama dengan nama BERBEDA =
    # ambigu: menentukan mana yang benar berarti menebak, jadi
    # ditolak.
    kunci_nama_baris = {_kunci_nama(r.get("nama")) for r in cocok_id}
    if len(kunci_nama_baris) > 1:
        return (BELUM, "nama_kembar_beda_id")

    kunci_profil = _kunci_nama(nama_profil)
    kunci_excel = _kunci_nama(cocok_id[0].get("nama"))
    if kunci_profil is not None and kunci_profil == kunci_excel:
        return (_STATUS_OK, "ok")
    return (BELUM, "nama_tidak_cocok")


def prodi_pemilik(baris_referensi, scholar_id_run, nama_profil):
    """Ambil prodi pemilik profil dari Excel referensi, atau None.

    Gerbangnya SAMA dengan verifikasi_pemilik_profil (K8): id eksak
    (strip), nama cocok setelah _kunci_nama, id ambigu ditolak -
    TANPA fuzzy matching, TANPA menebak. Nilai `prodi` baris Excel
    yang lolos gerbang dikembalikan (strip; kosong -> None).

    Mengembalikan None kalau verifikasi gagal (alasan apa pun),
    kolom `prodi` tidak ada / kosong / hanya putih, atau baris
    referensi tidak berbentuk dict. Tidak pernah melempar untuk
    data yang buruk - sama seperti verifikasi_pemilik_profil. None
    disimpan sebagai NULL di raw.scholar_author.prodi (nullable).
    """
    status, _alasan = verifikasi_pemilik_profil(
        nama_profil, scholar_id_run, baris_referensi)
    if status != _STATUS_OK:
        return None

    id_bersih = str(scholar_id_run).strip()
    for r in baris_referensi:
        baris = r if isinstance(r, dict) else {}
        if str(baris.get("scholar_id") or "").strip() == id_bersih:
            prodi = str(baris.get("prodi") or "").strip()
            return prodi or None
    # Tidak tercapai: verifikasi lolos berarti minimal satu baris
    # dengan id sama ada di referensi.
    return None  # pragma: no cover


def posisi_pemilik(daftar_nama, nama_profil, sumber):
    """Kumpulkan SEMUA indeks di daftar_nama yang cocok dgn pemilik.

    Rantai pencocokan per posisi berhenti pada kecocokan pertama:
      (a) eksak setelah _kunci_nama,
      (b) bentuk pendek/inisial ("TAD Kuntjoro" <-> "Tri Agus Djoko
          Kuntjoro") lewat _bentuk_pendek,
      (c) varian nama dipotong per kata ("Tri Agus D Kuntjoro")
          lewat _varian_nama_pemilik - HANYA untuk sumber "detail".

    Varian (c) sengaja dimatikan di luar jalur detail: di kolom
    authors halaman profil daftar sudah terpotong, dan longgarnya
    pencocokan di sana bisa menempelkan orang yang salah (K8).

    Mengembalikan set indeks POSITIF, bukan hanya yang pertama:
    pemilik boleh muncul lebih dari satu kali dalam satu publikasi
    (D-7) dan SEMUA kemunculannya berhak `pemilik_profil`. Nama
    profil kosong -> set kosong (tidak ada yang boleh ditandai).
    """
    hasil = set()
    kunci_profil = _kunci_nama(nama_profil)
    if not kunci_profil:
        return hasil
    for i, nama in enumerate(daftar_nama or []):
        kunci = _kunci_nama(nama)
        if kunci is not None and kunci == kunci_profil:
            hasil.add(i)
        elif _bentuk_pendek(nama, nama_profil):
            hasil.add(i)
        elif sumber == SUMBER_DETAIL and _varian_nama_pemilik(nama, nama_profil):
            hasil.add(i)
    return hasil


def bangun_record_author(teks_daftar, sumber, nama_profil=None,
                         baris_referensi=None, scholar_id_run=None):
    """Susun list record author per publikasi, sesuai urutan sumber.

    Setiap record (kunci persis, urutan tidak berpengaruh pada
    perbandingan dict):

        author_order           int 0-based, urutan tampil Scholar (K4)
        author_name            nama APA ADANYA hasil _pecah_penulis -
                               tanpa recasing, tanpa dibalik, tanpa
                               dipertebal (K2)
        author_role            penulis_pertama/co_author dari urutan (K5)
        profile_owner_status   pemilik_profil/bukan_pemilik_profil/
                               belum_terverifikasi (K6)
        scholar_id             id run ini HANYA untuk record
                               pemilik_profil; selain itu None
                               (tidak ada tebakan)
        sumber                 "detail" | "daftar" (D-1)
        daftar_terpotong       bool, True hanya untuk sumber "daftar"
                               yang mengandung elipsis (D-1)

    Aturan:
      * `sumber` di luar "detail"/"daftar" = error PEMROGRAMAN ->
        ValueError (bukan data yang buruk).
      * Daftar kosong -> [] : tidak pernah mengarang nama.
      * Verifikasi gagal -> SEMUA record belum_terverifikasi dan
        scholar_id None (K8); tidak ada satu pun pemilik_profil.
      * Verifikasi berhasil -> posisi pemilik lewat posisi_pemilik
        (dengan aturan varian per-sumber), sisanya
        bukan_pemilik_profil.
      * Deterministik dan tidak memutasi argumen input.
    """
    if sumber not in (SUMBER_DETAIL, SUMBER_DAFTAR):
        raise ValueError(
            "sumber tidak dikenal: {!r} (harus {!r} atau {!r})".format(
                sumber, SUMBER_DETAIL, SUMBER_DAFTAR))

    daftar = _pecah_penulis(teks_daftar or "")
    if not daftar:
        return []

    terpotong = ((sumber == SUMBER_DAFTAR)
                 and deteksi_daftar_terpotong(teks_daftar))

    status, _alasan = verifikasi_pemilik_profil(
        nama_profil, scholar_id_run, baris_referensi)

    if status == _STATUS_OK:
        cocok = posisi_pemilik(daftar, nama_profil, sumber)
        id_bersih = str(scholar_id_run).strip()
    else:
        cocok = set()
        id_bersih = None

    rekaman = []
    for i, nama in enumerate(daftar):
        if status != _STATUS_OK:
            # Gagal verifikasi: tidak ada yang boleh ditandai
            # pemilik, berapa pun kemiripan namanya (K8).
            kepemilikan, id_penulis = BELUM, None
        elif i in cocok:
            kepemilikan, id_penulis = PEMILIK, id_bersih
        else:
            kepemilikan, id_penulis = BUKAN, None
        rekaman.append({
            "author_order": i,
            "author_name": nama,
            "author_role": peran_dari_urutan(i),
            "profile_owner_status": kepemilikan,
            "scholar_id": id_penulis,
            "sumber": sumber,
            "daftar_terpotong": terpotong,
        })
    return rekaman
