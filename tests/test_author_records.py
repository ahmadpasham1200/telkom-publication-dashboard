"""Test untuk src/loading/author_records.py.

Cakupan file ini: helper PURE penyusun record author per publikasi
untuk raw.scholar_publication_author (desain P56). SEMUA data di
bawah sintetis - tidak membaca file Excel nyata, tidak menyentuh
jaringan, tidak memakai direktori fixtures, dan tidak membuka
database.

Yang paling dijaga di sini adalah kontrak anti-tebakan:
    - identitas belum terverifikasi -> nol pemilik_profil, nol
      scholar_id (K8),
    - nama penulis persis seperti hasil _pecah_penulis (K2),
    - author_role murni dari urutan dan boleh berdiri sendiri
      dari profile_owner_status (K5/K7).

Cara menjalankan:
    python -m pytest tests/test_author_records.py -q
"""

import copy
import sys
from pathlib import Path

# --- Bootstrap import -------------------------------------------------
# Repo ini tidak punya __init__.py di mana pun (implicit namespace
# package), jadi `python -m pytest` dari root sudah cukup. Sisanya
# tetap ditambahkan supaya `pytest tests/...` (tanpa -m) juga
# bekerja dari direktori mana pun.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pytest  # noqa: E402

from src.loading.author_records import (  # noqa: E402
    BELUM,
    BUKAN,
    PEMILIK,
    ROLE_CO,
    ROLE_PERTAMA,
    SUMBER_DAFTAR,
    SUMBER_DETAIL,
    bangun_record_author,
    deteksi_daftar_terpotong,
    peran_dari_urutan,
    posisi_pemilik,
    prodi_pemilik,
    verifikasi_pemilik_profil,
)


# ============================================================
# PERAN DARI URUTAN (K5)
# ============================================================

def test_peran_hanya_dari_urutan():
    """Urutan 0 = penulis_pertama, sisanya co_author tanpa syarat."""
    assert peran_dari_urutan(0) == ROLE_PERTAMA
    assert peran_dari_urutan(1) == ROLE_CO
    assert peran_dari_urutan(5) == ROLE_CO


# ============================================================
# PENYUSUNAN RECORD DASAR
# ============================================================

def test_satu_penulis_menghasilkan_satu_record():
    """Satu nama -> satu record, urutan 0, peran penulis pertama."""
    hasil = bangun_record_author("Budi Santoso", SUMBER_DETAIL)
    assert len(hasil) == 1
    rek = hasil[0]
    assert rek["author_order"] == 0
    assert rek["author_role"] == ROLE_PERTAMA
    assert rek["author_name"] == "Budi Santoso"
    assert rek["sumber"] == SUMBER_DETAIL
    assert rek["daftar_terpotong"] is False


def test_enam_penulis_urutan_dan_peran_lengkap():
    """Enam nama: urutan 0..5, hanya yang pertama penulis_pertama."""
    teks = ("Budi Santoso, Siti Rahayu, Tono Wibowo, Andi Pratama, "
            "Maya Lestari, Joko Susilo")
    hasil = bangun_record_author(teks, SUMBER_DETAIL)
    assert [r["author_order"] for r in hasil] == [0, 1, 2, 3, 4, 5]
    assert hasil[0]["author_role"] == ROLE_PERTAMA
    assert all(r["author_role"] == ROLE_CO for r in hasil[1:])
    assert [r["author_name"] for r in hasil] == teks.split(", ")
    assert all(r["sumber"] == SUMBER_DETAIL for r in hasil)
    assert all(r["daftar_terpotong"] is False for r in hasil)


def test_urutan_penulis_banyak_tidak_berubah():
    """Daftar panjang realistis: urutan record = urutan teks sumber."""
    teks = ("Rina Wati, Agus Salim, TAD Kuntjoro, Dewi Anggraini, "
            "Budi Santoso, Hasan Basri, Siti Rahayu, Joko Susilo")
    urutan_diharapkan = [
        "Rina Wati", "Agus Salim", "TAD Kuntjoro", "Dewi Anggraini",
        "Budi Santoso", "Hasan Basri", "Siti Rahayu", "Joko Susilo",
    ]
    hasil = bangun_record_author(teks, SUMBER_DETAIL)
    assert [r["author_name"] for r in hasil] == urutan_diharapkan
    assert [r["author_order"] for r in hasil] == list(range(8))


def test_inisial_penulis_tidak_diperluas():
    """Bentuk inisial dipertahankan - bukan ditebak jadi nama panjang."""
    hasil = bangun_record_author("TAD Kuntjoro, Siti Rahayu", SUMBER_DETAIL)
    assert hasil[0]["author_name"] == "TAD Kuntjoro"


def test_tanda_baca_kapital_dan_spasi_tetap_sesuai_sumber():
    """Titik dalam nama tidak memecah, kapital dan tepi spasi aman."""
    teks = "A. Budi,  Prof. Siti , M. Kuntjoro, Ph.D. Rahayu"
    hasil = bangun_record_author(teks, SUMBER_DETAIL)
    assert [r["author_name"] for r in hasil] == [
        "A. Budi", "Prof. Siti", "M. Kuntjoro", "Ph.D. Rahayu",
    ]


# ============================================================
# VERIFIKASI PEMILIK PROFIL (gerbang Excel)
# ============================================================

def test_verifikasi_eksak_menghasilkan_terverifikasi():
    """Cocok eksak setelah normalisasi -> terverifikasi/ok."""
    baris = [{"no": 1, "nama": " Budi  Santoso ", "scholar_id": "ID1"}]
    assert verifikasi_pemilik_profil("Budi Santoso", "ID1", baris) == (
        "terverifikasi", "ok")


def test_referensi_dan_input_kosong_memberi_alasan_sendiri():
    """Baris referensi/id/nama kosong -> alasan masing-masing, bukan ditebak."""
    baris = [{"no": 1, "nama": "Budi Santoso", "scholar_id": "ID1"}]
    assert verifikasi_pemilik_profil("Budi Santoso", "ID1", None) == (
        BELUM, "referensi_tidak_ada")
    assert verifikasi_pemilik_profil("Budi Santoso", "ID1", []) == (
        BELUM, "referensi_tidak_ada")
    # id run kosong didahulukan: tanpa id tidak ada yang bisa dicari.
    assert verifikasi_pemilik_profil("Budi Santoso", "", baris) == (
        BELUM, "id_run_kosong")
    assert verifikasi_pemilik_profil("Budi Santoso", None, baris) == (
        BELUM, "id_run_kosong")
    assert verifikasi_pemilik_profil("Budi Santoso", "   ", baris) == (
        BELUM, "id_run_kosong")
    assert verifikasi_pemilik_profil("", "ID1", baris) == (
        BELUM, "nama_profil_kosong")
    assert verifikasi_pemilik_profil(None, "ID1", baris) == (
        BELUM, "nama_profil_kosong")
    assert verifikasi_pemilik_profil("   ", "ID1", baris) == (
        BELUM, "nama_profil_kosong")


def test_id_run_tidak_ada_di_referensi():
    """ID9 tidak ada di Excel -> id_tidak_ditemukan, semua record gagal."""
    baris = [{"no": 1, "nama": "Budi Santoso", "scholar_id": "ID1"}]
    assert verifikasi_pemilik_profil("Budi Santoso", "ID9", baris) == (
        BELUM, "id_tidak_ditemukan")

    hasil = bangun_record_author(
        "Budi Santoso, Siti Rahayu", SUMBER_DETAIL,
        nama_profil="Budi Santoso", baris_referensi=baris,
        scholar_id_run="ID9")
    assert all(r["profile_owner_status"] == BELUM for r in hasil)
    assert all(r["scholar_id"] is None for r in hasil)


def test_id_sama_nama_kembar_ditolak_sebagai_ambigu():
    """ID sama dengan dua nama berbeda -> tidak ditebak, ditolak."""
    baris = [
        {"no": 1, "nama": "Budi Santoso", "scholar_id": "ID1"},
        {"no": 2, "nama": "Budi A. Santoso", "scholar_id": "ID1"},
    ]
    assert verifikasi_pemilik_profil("Budi Santoso", "ID1", baris) == (
        BELUM, "nama_kembar_beda_id")

    hasil = bangun_record_author(
        "Budi Santoso, Siti Rahayu", SUMBER_DETAIL,
        nama_profil="Budi Santoso", baris_referensi=baris,
        scholar_id_run="ID1")
    assert all(r["profile_owner_status"] == BELUM for r in hasil)
    assert all(r["scholar_id"] is None for r in hasil)


def test_id_sama_nama_beda_tidak_cocok_tanpa_penebakan():
    """Nama profil tidak cocok dgn nama Excel pada id sama (K8)."""
    baris = [{"no": 1, "nama": "Other Person", "scholar_id": "ID1"}]
    assert verifikasi_pemilik_profil("Budi Santoso", "ID1", baris) == (
        BELUM, "nama_tidak_cocok")

    hasil = bangun_record_author(
        "Budi Santoso, Siti Rahayu", SUMBER_DETAIL,
        nama_profil="Budi Santoso", baris_referensi=baris,
        scholar_id_run="ID1")
    assert all(r["profile_owner_status"] == BELUM for r in hasil)
    assert all(r["scholar_id"] is None for r in hasil)


def test_pemilik_di_urutan_pertama_verifikasi_eksak():
    """Pemilik di depan: record 0 pemilik + id, record 1 bukan + None."""
    baris = [{"no": 1, "nama": "Budi Santoso", "scholar_id": "ID1"}]
    hasil = bangun_record_author(
        "Budi Santoso, Siti Rahayu", SUMBER_DETAIL,
        nama_profil="Budi Santoso", baris_referensi=baris,
        scholar_id_run="ID1")
    r0, r1 = hasil
    assert r0["author_role"] == ROLE_PERTAMA
    assert r0["profile_owner_status"] == PEMILIK
    assert r0["scholar_id"] == "ID1"
    assert r1["author_role"] == ROLE_CO
    assert r1["profile_owner_status"] == BUKAN
    assert r1["scholar_id"] is None


def test_pemilik_di_urutan_tengah_hanya_satu_posisi():
    """Pemilik di posisi 1 (bukan pertama, K9): hanya indeks 1."""
    baris = [{"no": 1, "nama": "Budi Santoso", "scholar_id": "ID1"}]
    hasil = bangun_record_author(
        "Siti Rahayu, Budi Santoso, Tono Wibowo", SUMBER_DETAIL,
        nama_profil="Budi Santoso", baris_referensi=baris,
        scholar_id_run="ID1")
    assert [i for i, r in enumerate(hasil)
            if r["profile_owner_status"] == PEMILIK] == [1]
    assert hasil[0]["profile_owner_status"] == BUKAN
    assert hasil[2]["profile_owner_status"] == BUKAN


def test_pemilik_co_author_bentuk_pendek_berdiri_di_dua_kolom():
    """K7: record yang sama co_author SEKALIGUS pemilik_profil."""
    baris = [{"no": 1, "nama": "Tri Agus Djoko Kuntjoro",
              "scholar_id": "ID1"}]
    hasil = bangun_record_author(
        "Siti Rahayu, TAD Kuntjoro", SUMBER_DETAIL,
        nama_profil="Tri Agus Djoko Kuntjoro", baris_referensi=baris,
        scholar_id_run="ID1")
    rek = hasil[1]
    assert rek["author_role"] == ROLE_CO
    assert rek["profile_owner_status"] == PEMILIK
    assert rek["scholar_id"] == "ID1"
    assert rek["author_name"] == "TAD Kuntjoro"
    assert hasil[0]["profile_owner_status"] == BUKAN


# ============================================================
# ELIPSIS / SUMBER / KEKOSONGAN
# ============================================================

def test_deteksi_daftar_terpotong_kedua_ejaan_dan_kasus_kosong():
    """"..." data nyata dan "…" Unicode sama-sama terdeteksi."""
    assert deteksi_daftar_terpotong("A B, C D, ...") is True
    assert deteksi_daftar_terpotong("A B, C D, \u2026") is True
    assert deteksi_daftar_terpotong("A B, C D") is False
    # Titik pada nama biasa bukan elipsis.
    assert deteksi_daftar_terpotong("Ph.D. Rahayu, M. Kuntjoro") is False
    assert deteksi_daftar_terpotong(None) is False
    assert deteksi_daftar_terpotong("") is False


def test_elipsis_di_daftar_terpotong_tidak_jadi_nama():
    """Elipsis dipotong oleh _pecah_penulis dan ditandai terpotong."""
    hasil = bangun_record_author("A B, C D, ...", SUMBER_DAFTAR)
    assert len(hasil) == 2
    assert all(r["daftar_terpotong"] is True for r in hasil)
    assert [r["author_name"] for r in hasil] == ["A B", "C D"]
    assert "..." not in [r["author_name"] for r in hasil]


def test_sumber_daftar_fallback_tetap_bekerja():
    """Gagal detail -> pemanggil memakai string daftar, sumber ikut."""
    baris = [{"no": 1, "nama": "Tri Agus Djoko Kuntjoro",
              "scholar_id": "ID1"}]
    hasil = bangun_record_author(
        "Siti Rahayu, TAD Kuntjoro", SUMBER_DAFTAR,
        nama_profil="Tri Agus Djoko Kuntjoro", baris_referensi=baris,
        scholar_id_run="ID1")
    assert len(hasil) == 2
    assert all(r["sumber"] == SUMBER_DAFTAR for r in hasil)
    # Bentuk pendek (rantai b) berlaku di semua sumber.
    assert hasil[1]["profile_owner_status"] == PEMILIK
    assert hasil[1]["author_role"] == ROLE_CO


def test_input_kosong_menghasilkan_nol_record():
    """Daftar kosong -> [] : tidak ada nama yang dikarang."""
    assert bangun_record_author(None, SUMBER_DETAIL) == []
    assert bangun_record_author("", SUMBER_DAFTAR) == []
    assert bangun_record_author(" , , ", SUMBER_DETAIL) == []
    assert bangun_record_author("...", SUMBER_DETAIL) == []
    # Gagal verifikasi sekalipun, daftar kosong tetap [].
    assert bangun_record_author(
        None, SUMBER_DETAIL, nama_profil="Budi Santoso",
        baris_referensi=[{"nama": "Budi Santoso", "scholar_id": "ID1"}],
        scholar_id_run="ID1") == []


def test_sumber_tidak_dikenal_melempar_value_error():
    """Sumber di luar kontrak = error pemrograman, bukan data."""
    with pytest.raises(ValueError):
        bangun_record_author("Budi Santoso", "profil")
    with pytest.raises(ValueError):
        bangun_record_author("Budi Santoso", None)


# ============================================================
# DETERMINISME, ANTI-FABRIKASI, VARIAN
# ============================================================

def test_sama_dengan_sama_menghasilkan_hasil_sama_dan_input_tidak_berubah():
    """Deterministik: dua panggilan identik, input tidak dimutasi."""
    baris = [{"no": 1, "nama": "Budi Santoso", "scholar_id": "ID1"}]
    baris_sebelum = copy.deepcopy(baris)
    argumen = dict(
        teks_daftar="Budi Santoso, Siti Rahayu",
        sumber=SUMBER_DETAIL,
        nama_profil="Budi Santoso",
        baris_referensi=baris,
        scholar_id_run="ID1",
    )
    pertama = bangun_record_author(**argumen)
    kedua = bangun_record_author(**argumen)
    assert pertama == kedua
    assert pertama is not kedua
    assert baris == baris_sebelum


def test_verifikasi_gagal_nol_pemilik_nol_id_nol_fabrikasi():
    """Run gagal verifikasi: nol pemilik, nol id, nama persis sumber."""
    teks = "Budi Santoso, Siti Rahayu, Tono Wibowo"
    hasil = bangun_record_author(
        teks, SUMBER_DETAIL, nama_profil="Orang Asing",
        baris_referensi=[{"no": 1, "nama": "Budi Santoso",
                          "scholar_id": "ID1"}],
        scholar_id_run="ID9")
    assert len(hasil) == 3
    assert not any(r["profile_owner_status"] == PEMILIK for r in hasil)
    assert all(r["profile_owner_status"] == BELUM for r in hasil)
    assert all(r["scholar_id"] is None for r in hasil)
    assert [r["author_name"] for r in hasil] == teks.split(", ")
    assert [r["author_order"] for r in hasil] == [0, 1, 2]
    assert [r["author_role"] for r in hasil] == [
        ROLE_PERTAMA, ROLE_CO, ROLE_CO]


def test_varian_nama_hanya_di_jalur_detail():
    """Varian "Tri Agus D Kuntjoro": boleh di detail, mati di daftar."""
    baris = [{"no": 1, "nama": "Tri Agus Djoko Kuntjoro",
              "scholar_id": "ID1"}]
    argumen = dict(
        teks_daftar="Tri Agus D Kuntjoro",
        nama_profil="Tri Agus Djoko Kuntjoro",
        baris_referensi=baris,
        scholar_id_run="ID1",
    )
    detail = bangun_record_author(sumber=SUMBER_DETAIL, **argumen)
    assert detail[0]["profile_owner_status"] == PEMILIK
    assert detail[0]["scholar_id"] == "ID1"

    daftar = bangun_record_author(sumber=SUMBER_DAFTAR, **argumen)
    assert daftar[0]["profile_owner_status"] == BUKAN
    assert daftar[0]["scholar_id"] is None


def test_pemilik_bisa_muncul_di_banyak_posisi():
    """D-7: dua kemunculan pemilik sama-sama ditandai, tengah bukan."""
    baris = [{"no": 1, "nama": "TAD Kuntjoro", "scholar_id": "ID1"}]
    hasil = bangun_record_author(
        "TAD Kuntjoro, X Y, TAD Kuntjoro", SUMBER_DETAIL,
        nama_profil="TAD Kuntjoro", baris_referensi=baris,
        scholar_id_run="ID1")
    assert len(hasil) == 3
    assert hasil[0]["profile_owner_status"] == PEMILIK
    assert hasil[2]["profile_owner_status"] == PEMILIK
    assert hasil[1]["profile_owner_status"] == BUKAN
    assert [r["scholar_id"] for r in hasil] == ["ID1", None, "ID1"]


def test_posisi_pemilik_nama_kosong_kosong_semua():
    """Nama profil kosong -> tidak ada posisi yang boleh ditandai."""
    assert posisi_pemilik(["Budi Santoso", "TAD Kuntjoro"],
                          "", SUMBER_DETAIL) == set()
    assert posisi_pemilik(["Budi Santoso"], None, SUMBER_DAFTAR) == set()
    assert posisi_pemilik([], "Budi Santoso", SUMBER_DETAIL) == set()


# ============================================================
# PRODI PEMILIK PROFIL (gerbang Excel yang sama dengan verifikasi)
# ============================================================

def test_prodi_pemilik_terverifikasi_mengembalikan_nilai():
    """Id eksak + nama cocok -> nilai prodi strip; duplikat id+nama
    sama pakai baris pertama (konsisten dgn verifikasi)."""
    referensi = [
        {"no": 1, "nama": "Budi Santoso", "scholar_id": "ID1",
         "prodi": "Informatika"},
        {"no": 2, "nama": "Siti Aminah", "scholar_id": "ID2",
         "prodi": "Teknik Elektro"},
    ]
    assert prodi_pemilik(
        referensi, "ID1", "Budi Santoso") == "Informatika"
    assert prodi_pemilik(
        referensi, "ID2", "Siti Aminah") == "Teknik Elektro"
    # Strip nilai prodi yang berputih di Excel.
    referensi[0]["prodi"] = "  Informatika  "
    assert prodi_pemilik(
        referensi, "ID1", "Budi Santoso") == "Informatika"


def test_prodi_pemilik_gagal_verifikasi_none():
    """Referensi kosong / id kosong / nama profil kosong / id tidak
    ada / nama tidak cocok / id ambigu -> None (tanpa menebak)."""
    referensi = [
        {"no": 1, "nama": "Budi Santoso", "scholar_id": "ID1",
         "prodi": "Informatika"},
    ]
    # Referensi tidak ada sama sekali.
    assert prodi_pemilik(None, "ID1", "Budi Santoso") is None
    # Id run kosong.
    assert prodi_pemilik(referensi, None, "Budi Santoso") is None
    assert prodi_pemilik(referensi, "   ", "Budi Santoso") is None
    # Nama profil kosong.
    assert prodi_pemilik(referensi, "ID1", None) is None
    assert prodi_pemilik(referensi, "ID1", "  ") is None
    # Id tidak ditemukan.
    assert prodi_pemilik(referensi, "ID9", "Budi Santoso") is None
    # Nama tidak cocok (id sama, orang beda) - tanpa fuzzy.
    assert prodi_pemilik(referensi, "ID1", "Budi Santosoo") is None
    # Id sama dipakai dua nama berbeda -> ambigu.
    ambigu = referensi + [
        {"no": 3, "nama": "Orang Lain", "scholar_id": "ID1",
         "prodi": "Fisika"},
    ]
    assert prodi_pemilik(ambigu, "ID1", "Budi Santoso") is None


def test_prodi_pemilik_prodi_kosong_none():
    """Lolos verifikasi tapi kolom prodi kosong/putih/khilang -> None."""
    dasar = {"no": 1, "nama": "Budi Santoso", "scholar_id": "ID1"}
    # Kunci prodi tidak ada di dict (kontrak lama baca_daftar).
    assert prodi_pemilik([dasar], "ID1", "Budi Santoso") is None
    # Prodi kosong / hanya putih.
    assert prodi_pemilik(
        [dict(dasar, prodi="")], "ID1", "Budi Santoso") is None
    assert prodi_pemilik(
        [dict(dasar, prodi="   ")], "ID1", "Budi Santoso") is None
