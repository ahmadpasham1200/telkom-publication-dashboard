"""
============================================================
Scraping Google Scholar - Playwright (versi file .py)
============================================================

Versi .py dari notebook ujicobapt2.ipynb, khusus metode Playwright.

Modul ini BUKAN skrip mandiri. Ia tidak punya entry point dan tidak
dipanggil lewat `python scrape_scholar_playwright.py`; dipakai sebagai
library oleh pipeline:

    python src/pipeline/run_pipeline.py --id 8kDg_v4AAAAJ

Yang diimpor pipeline: scrape_scholar(), print_table(), MAX_BATCHES,
SCHOLAR_ID_DEFAULT. Kalau butuh menjalankan scraping sekali tanpa
database, panggil scrape_scholar() dari kode sendiri.

Butuh install sekali:
    pip install playwright beautifulsoup4
    python -m playwright install chromium

Lihat juga: KETERANGAN_SCRAPING.txt
============================================================
"""

import random
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import unquote, urlsplit, urljoin, parse_qs

# ------------------------------------------------------------
# Setting Unicode output (Windows default cp1252 bisa crash
# saat print judul publikasi yang mengandung karakter non-ASCII)
# ------------------------------------------------------------
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass


# ============================================================
# KONFIGURASI
# ============================================================

SCHOLAR_ID_DEFAULT = "8kDg_v4AAAAJ"

# Nilai status yang mungkin dikembalikan oleh scrape_scholar().
# RATE_LIMITED = HTTP 429 teramati (navigasi awal / paging / detail):
# sinyal first-class supaya pemanggil batch tahu WAJIB menghentikan
# seluruh batch - bukan timeout biasa, bukan sekadar partial.
RUN_STATUSES = (
    "SUCCESS",
    "PARTIAL_SUCCESS",
    "BLOCKED",
    "TIMEOUT",
    "NO_DATA",
    "PARSING_ERROR",
    "RATE_LIMITED",
)

# Domain Scholar. Kalau domain ini kena blokir, ganti manual ke salah
# satu domain di bawah ini lalu jalankan ulang programnya.
DOMAINS = [
    "scholar.google.co.id",
    "scholar.google.com",
    "scholar.google.co.uk",
    "scholar.google.de",
    "scholar.google.co.jp",
]

# Google Scholar memaksa halaman profil ke 20 baris per muat, jadi
# parameter pagesize di URL tidak berpengaruh. Batas ini hanya
# pengaman supaya tidak klik "Tampilkan lainnya" tanpa henti.
MAX_BATCHES = 60

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

TIMEOUT_MS = 20000
JITTER_MIN = 15.0
JITTER_MAX = 25.0

# Jeda antar pengambilan halaman detail (lebih lebar dari jitter
# paging karena tiap publikasi = satu navigasi tambahan).
JITTER_DETAIL_MIN = 20.0
JITTER_DETAIL_MAX = 30.0

# Kunci field halaman detail yang digabung ke tiap baris rows[].
DETAIL_KEYS = (
    "detail_title", "detail_authors", "detail_date", "detail_venue",
    "detail_venue_label", "detail_volume", "detail_issue", "detail_pages",
    "detail_publisher", "detail_description", "detail_citations",
    "detail_link", "detail_pdf_link", "detail_doi",
    "detail_journal", "detail_conference", "detail_book",
    "detail_publication_type", "detail_extraction_status",
    "detail_cited_by", "detail_related_articles", "detail_all_versions",
)


# ============================================================
# HELPER
# ============================================================

def _utc_now_iso():
    """Waktu sekarang dalam ISO-8601 UTC, contoh: 2026-09-29T08:14:02Z."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _classify_status(*, has_data, blocked, network_error, parse_error,
                     expected_empty, partial, rate_limited=False):
    """Tentukan status run. Urutan evaluasi: first match wins.

    0. rate_limited (HTTP 429 teramati, apa pun fasenya) -> RATE_LIMITED
       (precedence TERTINGGI: sinyal untuk pemanggil batch berhenti
       seluruh batch, menang atas blocked/network_error/partial)
    1. fatal, tanpa data, captcha/unusual traffic  -> BLOCKED
    2. fatal, tanpa data, nav error / timeout      -> TIMEOUT
    3. fatal, tanpa data, parse HTML melempar error -> PARSING_ERROR
    4. fatal, profil termuat tapi 0 publikasi      -> NO_DATA
    5. ada data, sebagian gagal / paging terpotong -> PARTIAL_SUCCESS
    6. ada data, semua lancar                      -> SUCCESS
    """
    if rate_limited:
        return "RATE_LIMITED"
    if not has_data:
        if blocked:
            return "BLOCKED"
        if network_error:
            return "TIMEOUT"
        if parse_error:
            return "PARSING_ERROR"
        if expected_empty:
            return "NO_DATA"
        # Tidak ada data tanpa alasan spesifik: perlakukan sebagai NO_DATA.
        return "NO_DATA"
    if partial:
        return "PARTIAL_SUCCESS"
    return "SUCCESS"


def _make_data(scholar_id, name, h_index, rows, failures,
               domain, batches, elapsed):
    """Bentuk bagian `data` dari run envelope.

    Selain isi publikasi, `data` juga menyimpan diagnostik run: domain
    Scholar yang dipakai, jumlah batch yang termuat, dan durasi eksekusi
    dalam detik. Diagnostik ini sengaja diletakkan di dalam `data`,
    bukan di envelope, karena bentuk envelope dikunci 9 kunci.
    """
    return {
        "scholar_id": scholar_id,
        "name": name,
        "h_index": h_index,
        "rows": rows,
        "failures": failures,
        # ---------- diagnostik run ----------
        "domain": domain,
        "batches": batches,
        "elapsed": elapsed,
    }


def _make_envelope(*, run_id, started_at, status, records_fetched,
                   records_failed, error_message, data):
    """Bentuk run envelope 9 kunci yang dikembalikan scrape_scholar()."""
    return {
        "run_id": run_id,
        "source": "google_scholar",
        "started_at": started_at,
        "finished_at": _utc_now_iso(),
        "status": status,
        "records_fetched": records_fetched,
        "records_failed": records_failed,
        "error_message": error_message,
        "data": data,
    }


def _collect_rows(parsed_rows, failures, start_index):
    """Pisahkan baris valid dari baris tanpa judul.

    Judul adalah kolom NOT NULL di hilir, jadi baris yang terparse tapi
    tidak punya judul dianggap gagal: dicatat ke `failures` dan TIDAK
    dimasukkan ke daftar baris valid. Iterasi tetap lanjut ke baris
    berikutnya.

    `start_index` adalah posisi global baris pertama di `parsed_rows`
    dalam seluruh aliran baris yang sudah diparse (lintas batch), supaya
    `index` pada tiap failure unik dan bisa dilacak.
    """
    valid = []
    for offset, row in enumerate(parsed_rows):
        if not row.get("title"):
            failures.append({
                "index": start_index + offset,
                "raw_title": row.get("title"),
                "reason": "missing_title",
            })
            continue
        valid.append(row)
    return valid


def _detail_kosong():
    """Dict field detail bernilai None (detail gagal / tidak dicoba)."""
    return {k: None for k in DETAIL_KEYS}


def _parse_tahun_daftar(year_text):
    """Ambil tahun 4 digit pertama dari teks tahun halaman daftar.

    Contoh: "2024" -> 2024, "2024-2025" -> 2024, "in press" -> None.
    """
    if not year_text:
        return None
    cocok = re.search(r"(19|20)\d{2}", year_text)
    return int(cocok.group(0)) if cocok else None


# ============================================================
# BIBLIOGRAFI PROFIL (volume/issue/pages dari teks venue/extra)
# ============================================================

# Pola di AKHIR teks, dicocokkan berurutan: tahun -> halaman ->
# volume (issue). Semua pola berbasis bukti fixture
# fixtures/scholar/profil_hlid.html; lihat docstring
# _ekstrak_bibliografi_profil untuk batasan yang disengaja.
_POLA_TAHUN_AKHIR = re.compile(
    r",\s*((?:19|20)\d{2})(?:\s*-\s*(?:19|20)\d{2})?\s*$"
)
_POLA_HALAMAN_AKHIR = re.compile(
    r",\s*(\d{1,5})\s*[-–—]\s*(\d{1,5})\s*$"
)
_POLA_VOL_ISSUE_AKHIR = re.compile(
    r"\s(\d{1,4})\s*\(\s*(\d{1,4})\s*\)\s*$"
)
_POLA_ANGKA_TAHUN = re.compile(r"^(?:19|20)\d{2}$")


def _bibliografi_dari_teks(teks):
    """Parse SATU sumber (venue ATAU extra) -> volume/issue/pages.

    Teks dinormalisasi HANYA untuk pencocokan (NBSP -> spasi biasa,
    spasi ganda dirapikan). Fungsi ini tidak menyimpan/mengembalikan
    teks apa pun sehingga venue/extra asli tidak pernah berubah.
    """
    hasil: dict = {"volume": None, "issue": None, "pages": None}
    if not isinstance(teks, str) or not teks.strip():
        return hasil

    # Ruang di depan supaya pola volume/issue (yang diawali \s) tetap
    # cocok walau teks diawali langsung dengan angka volume.
    calon = " " + re.sub(r"\s+", " ", teks.replace("\xa0", " "))

    # 1) Buang segmen tahun di akhir: ", 2024" atau ", 2020-2021".
    calon = _POLA_TAHUN_AKHIR.sub("", calon)

    # 2) Rentang halaman di akhir: ", 23-32" (hyphen/en dash/em dash).
    #    DITOLAK bila kedua sisi berpola tahun (19xx/20xx) supaya
    #    rentang tahun tidak pernah menjadi "halaman".
    cocok = _POLA_HALAMAN_AKHIR.search(calon)
    if cocok:
        kiri, kanan = cocok.group(1), cocok.group(2)
        keduanya_tahun = (
            _POLA_ANGKA_TAHUN.fullmatch(kiri)
            and _POLA_ANGKA_TAHUN.fullmatch(kanan)
        )
        if not keduanya_tahun:
            hasil["pages"] = f"{kiri}-{kanan}"
            calon = _POLA_HALAMAN_AKHIR.sub("", calon)

    # 3) Volume + issue di akhir: "<volume> (<issue>)". Volume yang
    #    berpola tahun DITOLAK (tahun tidak boleh jadi volume);
    #    issue tetap dipakai selama bentuknya cocok.
    cocok = _POLA_VOL_ISSUE_AKHIR.search(calon)
    if cocok:
        volume, issue = cocok.group(1), cocok.group(2)
        if not _POLA_ANGKA_TAHUN.fullmatch(volume):
            hasil["volume"] = volume
        hasil["issue"] = issue

    return hasil


def _ekstrak_bibliografi_profil(venue, extra):
    """Ekstrak volume/issue/pages dari teks venue (dan extra) profil.

    Pola yang DIDUKUNG (berjangkar di AKHIR teks venue/extra):

        "... <volume> (<issue>)[, <rentang-halaman>][, <tahun>]"

    Aturan konservatif (ragu-ragu -> None):
      - segmen tahun di akhir (", 2024" atau rentang ", 2020-2021")
        dibuang lebih dulu supaya tidak tertukar dengan halaman/volume;
      - rentang halaman (", 23-32") dikenali dengan hyphen, en dash,
        atau em dash dan disimpan seragam "a-b"; rentang SATU angka
        ("23") TIDAK didukung -> None (tidak ada bukti fixture);
      - volume+issue harus bersuku "5 (2)"; volume berpola tahun
        (19xx/20xx) DITOLAK; volume tanpa issue ("X5, 2024") TIDAK
        didukung -> None (tidak ada bukti fixture);
      - sumber: `venue` dicoba lebih dulu; field yang masih None boleh
        diisi dari `extra`; venue menang bila keduanya punya data;
      - NBSP dan spasi ganda dinormalisasi HANYA untuk pencocokan;
        teks venue/extra yang disimpan di baris tidak diubah;
      - helper hanya menerima venue/extra - tidak pernah membaca judul
        dan tidak pernah menulis key apa pun yang diawali "detail_".

    BATASAN: hanya pola di atas yang diklaim didukung. Ini BUKAN parser
    untuk semua format Google Scholar; format lain (mis. volume tanpa
    issue, halaman tunggal, atau metadata di tengah teks) menghasilkan
    None daripada menebak.
    """
    hasil: dict = {"volume": None, "issue": None, "pages": None}
    for sumber in (venue, extra):
        for kunci, nilai in _bibliografi_dari_teks(sumber).items():
            # Venue jadi sumber utama (diurutkan lebih dulu); extra
            # hanya mengisi field yang masih None.
            if hasil[kunci] is None:
                hasil[kunci] = nilai
    return hasil


# Pola DOI: prefix "10." + 4-9 digit registran + "/" + suffix non-spasi.
# Suffix membatasi karakter kutip/angle-bracket supaya tidak melarut ke
# teks sekitarnya; tanda baca penutup dibersihkan dulu. Dipakai di path
# (satu match) DAN di query (semua match, dipotong pada '&' - lihat
# _doi_dari_url). '#' fragment tetap dibuang sebelum pencarian.
_POLA_DOI = re.compile(r"10\.[0-9]{4,9}/[^\s\"'<>]+")


def _norm_doi(doi):
    """Normalisasi DOI untuk PERBANDINGAN saja (lowercase + buang tanda
    baca penutup). Nilai yang disimpan tetap bentuk verbatim."""
    return doi.rstrip(".,;:)]}\"'").lower()


# Ekstensi berkas yang dikenal. Dipakai HANYA untuk kunci perbandingan /
# pemilihan representasi; BUKAN untuk memangkas nilai yang disimpan dari
# URL tunggal (lihat batasan di _doi_dari_url).
_EKSTENSI_BERKAS = (".html", ".htm", ".xhtml", ".pdf", ".php", ".aspx")


def _tanpa_ekstensi_berkas(doi):
    """Buang SATU ekstensi berkas yang dikenal di ujung (case-insensitive).

    Hanya untuk perbandingan/pemilihan; tidak dipakai memangkas nilai
    yang disimpan dari URL tunggal.
    """
    rendah = doi.lower()
    for ext in _EKSTENSI_BERKAS:
        if rendah.endswith(ext):
            return doi[: -len(ext)]
    return doi


def _kunci_doi(doi):
    """Kunci perbandingan DOI (BUKAN nilai simpan).

    lowercase + buang tanda baca penutup + buang satu ekstensi berkas yang
    dikenal (+ tanda baca penutup lagi). Dipakai memutuskan apakah dua URL
    menghasilkan DOI yang SAMA, tanpa mengubah nilai yang disimpan.
    """
    bersih = _norm_doi(doi)
    bersih = _tanpa_ekstensi_berkas(bersih)
    return _norm_doi(bersih)


def _punya_ekstensi_berkas(doi):
    rendah = doi.lower()
    return any(rendah.endswith(ext) for ext in _EKSTENSI_BERKAS)


def _jml_penutup(doi):
    return len(doi) - len(doi.rstrip(".,;:)]}\"'"))


def _pilih_doi_representatif(a, b):
    """Pilih SATU nilai simpan dari dua kandidat yang kuncinya sama.

    Konservatif: hanya memilih yang lebih bersih bila dasarnya jelas.
    Kembalikan None bila ambigu (dua-duanya berakhiran ekstensi berkas
    yang berbeda) supaya pemanggil memperlakukannya sebagai konflik,
    bukan menyatukan diam-diam.
    """
    if a == b:
        return a
    a_ext = _punya_ekstensi_berkas(a)
    b_ext = _punya_ekstensi_berkas(b)
    if a_ext and not b_ext:
        return b
    if b_ext and not a_ext:
        return a
    if not a_ext and not b_ext:
        return a if _jml_penutup(a) <= _jml_penutup(b) else b
    # dua-duanya berakhiran ekstensi berkas
    if _norm_doi(a) == _norm_doi(b):
        return a
    return None


def _doi_dari_url(url):
    """Ambil DOI dari satu URL; None kalau tidak ada.

    Pencarian BERTAHAP, PATH didahulukan di atas query:

      1. PATH: fragment '#' & query '?' dipotong lebih dulu, '%xx'
         di-decode bila ada, lalu pattern dicari (satu match).
      2. QUERY (dukungan aman P11): hanya bila path tidak menghasilkan
         DOI. Bagian setelah '?' pertama (fragment '#' TETAP dibuang
         lebih dulu -> #fragment TIDAK didukung), '%xx' di-decode bila
         ada, lalu SEMUA match pattern dicari; TIAP match dipotong pada
         '&' pertama (proteksi over-capture: '10.x/y&return=z' ->
         '10.x/y') lalu dibersihkan dengan rstrip(",]}") yang sama;
         kandidat PERTAMA yang non-kosong menang (first-match, konsisten
         dengan jalur path).

    Hanya karakter penutup yang TIDAK sah dalam suffix DOI ("," "]" "}")
    yang dibuang. Karakter ".", ";", ":", ")" dipertahankan karena SAH
    sebagai bagian DOI, supaya DOI valid tidak terpangkas.

    BATASAN DIKETAHUI: suffix berkas seperti ".html"/".pdf" tersusun dari
    karakter suffix DOI yang sah, sehingga tidak dapat dibedakan dari DOI
    asli secara andal -> TIDAK dipotong (menghindari menebak) - berlaku
    sama di path MAUPUN query. Bila URL benar-benar menempelkan ekstensi
    berkas SETELAH DOI, hasil bisa over-capture.

    Kontrak _ekstrak_doi (dua sumber, kunci banding, konflik) TIDAK
    berubah; fungsi ini hanya memperluas DI MANA DOI bisa ditemukan.
    """
    if not isinstance(url, str) or "10." not in url:
        return None
    tanpa_fragment = url.split("#", 1)[0]

    # 1) PATH didahulukan; bila menghasilkan DOI, query diabaikan.
    path = tanpa_fragment.split("?", 1)[0]
    if "%" in path:
        path = unquote(path)
    cocok = _POLA_DOI.search(path)
    if cocok:
        # Buang HANYA penutup yang tidak sah dalam suffix DOI. ".", ";",
        # ":", ")" sah sebagai karakter DOI -> tidak dipotong.
        return cocok.group(0).rstrip(",]}")

    # 2) QUERY (dukungan aman P11) - hanya bila path kosong hasilnya.
    if "?" not in tanpa_fragment:
        return None
    query = tanpa_fragment.split("?", 1)[1]
    if "%" in query:
        query = unquote(query)
    for kandidat in _POLA_DOI.findall(query):
        # Proteksi over-capture: potong pada '&' pertama; lalu bersihkan
        # penutup tidak sah (rutin yang sama dengan jalur path).
        bersih = kandidat.split("&", 1)[0].rstrip(",]}")
        if bersih:
            return bersih  # first-match, konsisten dengan path
    return None


def _ekstrak_doi(detail_link, detail_pdf_link):
    """Tentukan SATU DOI dari dua URL halaman detail.

    Aturan (disepakati):
      - kedua URL menghasilkan DOI sama  -> DOI itu
      - hanya satu URL menghasilkan DOI  -> DOI itu
      - kedua URL menghasilkan DOI sama setelah kunci banding (case-insensitive,
        tanda baca penutup + ekstensi berkas dikenal dibuang untuk banding)
        -> nilai simpan yang lebih bersih
      - kedua URL menghasilkan DOI BEDA, atau ekstensi berkas yang berbeda di
        kedua sisi (ambigu) -> None + info konflik
      - tidak ada DOI                    -> None

    Nilai balik: (doi, konflik). konflik None, atau dict dengan kunci
    'detail_link_doi' & 'detail_pdf_doi' berisi DOI yang bertabrakan.
    TIDAK ada kolom database untuk status konflik; pemanggil mencatatnya
    lewat log.
    """
    doi_link = _doi_dari_url(detail_link)
    doi_pdf = _doi_dari_url(detail_pdf_link)
    if not doi_link and not doi_pdf:
        return None, None
    if not doi_pdf:
        return doi_link, None
    if not doi_link:
        return doi_pdf, None

    # Keduanya ada: bandingkan dengan kunci aman (case-insensitive, buang
    # tanda baca penutup + satu ekstensi berkas yang dikenal).
    if _kunci_doi(doi_link) == _kunci_doi(doi_pdf):
        pilihan = _pilih_doi_representatif(doi_link, doi_pdf)
        if pilihan is not None:
            return pilihan, None
    # DOI beda, ATAU sama tetapi representasinya ambigu (dua-duanya
    # berakhiran ekstensi berbeda) -> NULL + catat konflik.
    return None, {"detail_link_doi": doi_link, "detail_pdf_doi": doi_pdf}


def build_url(uid, start=0, domain=None, hl="id"):
    """URL profil publikasi.

    Parameter cstart & pagesize disertakan karena itu bentuk resmi
    URL profil, tapi pagesize TIDAK dipakai untuk paging. Google Scholar
    memaksa halaman profil mulai dari baris pertama apa pun nilai
    cstart-nya. Paging dilakukan lewat klik tombol "Tampilkan lainnya".
    """
    host = domain or DOMAINS[0]
    return (
        f"https://{host}/citations"
        f"?hl={hl}&user={uid}&cstart={start}&pagesize=100"
    )


def _parse_h_index(soup):
    """Ambil h-index dari tabel statistik profil (#gsc_rsb_st).

    Scholar menampilkan baris label/nilai; baris yang labelnya memuat
    "h-index" diambil nilai numerik pertamanya. Hanya h-index yang
    diambil di sini.
    """
    table = soup.select_one("#gsc_rsb_st")
    if table is None:
        return None

    for tr in table.select("tr"):
        cells = tr.find_all(["td", "th"])
        if not cells:
            continue
        label = cells[0].get_text(strip=True).lower()
        # Label statistik berbeda per bahasa: "h-index" (hl=en) dan
        # "indeks-h" (hl=id). Cocokkan keduanya.
        if "h-index" not in label and "indeks-h" not in label:
            continue
        for cell in cells[1:]:
            value = cell.get_text(strip=True)
            if value.isdigit():
                return int(value)
    return None


def parse_profile_html(html):
    """Parse HTML profil Google Scholar menjadi (nama, list of dict, h_index).

    Tiap baris `rows` juga membawa `publication_year` (int atau None,
    hasil ekstraksi tahun 4 digit dari kolom tahun) dan `detail_url`
    (href relatif dari fixture, mis.
    "/citations?view_op=view_citation&hl=id&user=...&citation_for_view=...",
    atau None; absolutisasi dilakukan di scrape_scholar).

    Tiga kunci bibliografi `volume`, `issue`, `pages` (str atau None)
    hasil `_ekstrak_bibliografi_profil(venue, extra)`; teks venue/extra
    yang disimpan tetap asli. Key ini TIDAK berkaitan dengan key
    `detail_*` dari halaman detail (lihat row.update(detail)).
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")

    name_tag = soup.select_one("#gsc_prf_in")
    name = name_tag.get_text(strip=True) if name_tag else None

    h_index = _parse_h_index(soup)

    rows = []
    for tr in soup.select("tr.gsc_a_tr"):
        title_tag = tr.select_one("a.gsc_a_at")
        grays = tr.select("div.gs_gray")
        cite_tag = tr.select_one("a.gsc_a_ac")
        year_tag = tr.select_one("span.gsc_a_h")

        cites_raw = cite_tag.get_text(strip=True) if cite_tag else ""
        cites = int(cites_raw) if cites_raw.isdigit() else 0

        # grays[0] = authors, grays[1] = venue, grays[2] = volume/number/pages
        authors = grays[0].get_text(strip=True) if len(grays) > 0 else None
        venue = grays[1].get_text(strip=True) if len(grays) > 1 else None
        extra = grays[2].get_text(strip=True) if len(grays) > 2 else None

        # volume/issue/pages diekstrak dari venue (utama) + extra
        # (pengisi field yang None); teks venue/extra di bawah tetap asli.
        bibliografi = _ekstrak_bibliografi_profil(venue, extra)

        rows.append({
            "title": title_tag.get_text(strip=True) if title_tag else None,
            "authors": authors,
            "venue": venue,
            "extra": extra,
            "citations": cites,
            "year": year_tag.get_text(strip=True) if year_tag else None,
            "publication_year": _parse_tahun_daftar(
                year_tag.get_text(strip=True) if year_tag else None
            ),
            "detail_url": title_tag.get("href") if title_tag else None,
            "volume": bibliografi["volume"],
            "issue": bibliografi["issue"],
            "pages": bibliografi["pages"],
        })

    return name, rows, h_index


def _ekstrak_sitasi_dari_blok(nilai_tag):
    """Ekstrak angka sitasi dari blok value 'Total kutipan' (murni DOM).

    Ekstraksi berbasis STRUKTUR, bukan teks label:
      - dipakai anchor <a href> di dalam blok yang href-nya mengandung
        'cites=';
      - anchor yang berada di dalam elemen id='gsc_oci_graph_wrapper'
        (histogram tahun/bar) DIKECUALIKAN walaupun href-nya juga
        mengandung 'cites=';
      - ANGKA PERTAMA pada teks anchor dipakai: "Dirujuk 3 kali" -> "3",
        "Dirujuk 0 kali" -> "0";
      - anchor cocok tapi teksnya tanpa angka -> None; tidak ada anchor
        yang cocok (struktur tidak sesuai) -> None. Konservatif: digit
        tahun histogram TIDAK pernah diambil, beberapa bagian tidak
        pernah digabung.

    CATATAN: ekstraktor ini tidak mengklaim dukungan bahasa Inggris
    ("Cited by"); belum ada bukti fixture-nya. Label blok yang tidak
    dikenal biarkan ditangani loop pemetaan (hasil tetap None).
    """
    if nilai_tag is None:
        return None
    for anchor in nilai_tag.select("a[href]"):
        href = anchor.get("href") or ""
        if "cites=" not in href:
            continue
        if anchor.find_parent(id="gsc_oci_graph_wrapper") is not None:
            continue
        cocok = re.search(r"\d[\d,.\s\u00a0]*", anchor.get_text())
        # Anchor pertama yang cocok dipakai; tanpa angka -> None.
        return re.sub(r"\D", "", cocok.group(0)) if cocok else None
    return None


# ============================================================
# LABEL METADATA HALAMAN DETAIL (Indonesia + alias non-fixture)
# ============================================================

# Satu sumber kebenaran label -> key hasil. Pencocokan PERSIS setelah
# normalisasi _norm_label (tanpa substring). Label Indonesia terbukti di
# fixture hl=id; alias Inggris NON-FIXTURE (belum diverifikasi ke HTML
# live) dan ditambahkan sebagai dukungan bilingual. Sengaja DITOLAK:
#   - "number": ambigu (bisa berarti hitungan apa pun; Scholar Inggris
#     memakai "Issue" untuk terbitan),
#   - "series": tidak cukup bukti untuk menentukan jenis publikasi.
# Label Book dan jenis venue spesifik ditangani oleh VENUE_FIELDS.
LABEL_DETAIL = {
    "detail_authors": ("pengarang", "penulis", "author(s)", "authors", "author"),
    "detail_date": ("tanggal terbit", "publication date", "date"),
    "detail_venue": ("jurnal", "konferensi", "journal", "conference", "venue"),
    "detail_volume": ("jilid", "volume", "vol"),
    "detail_issue": ("terbitan", "issue", "nomor"),
    "detail_pages": ("halaman", "pages", "page", "page range"),
    "detail_publisher": ("penerbit", "publisher"),
    "detail_citations": ("total kutipan", "cited by", "total citations"),
    "detail_doi": ("doi",),
}

# Explicit source labels only; a title or publisher alone is not type evidence.
VENUE_FIELDS = {
    'journal': ('detail_journal', 'journal'), 'jurnal': ('detail_journal', 'journal'),
    'conference': ('detail_conference', 'conference'),
    'konferensi': ('detail_conference', 'conference'),
    'proceedings': ('detail_conference', 'conference'),
    'prosiding': ('detail_conference', 'conference'),
    'book': ('detail_book', 'book'), 'buku': ('detail_book', 'book'),
    'book chapter': ('detail_book', 'book_chapter'),
    'bab buku': ('detail_book', 'book_chapter'),
    'thesis': ('detail_venue', 'thesis'), 'dissertation': ('detail_venue', 'thesis'),
    'tesis': ('detail_venue', 'thesis'), 'disertasi': ('detail_venue', 'thesis'),
    'skripsi': ('detail_venue', 'thesis'),
}

RELATION_FIELDS = {
    'cited by': 'detail_cited_by', 'artikel yang menyitasi': 'detail_cited_by',
    'related articles': 'detail_related_articles', 'artikel terkait': 'detail_related_articles',
    'all versions': 'detail_all_versions', 'semua versi': 'detail_all_versions',
}


def _parse_relations(soup, base_url):
    """Collect links already present in the DOM, without fetching targets.

    Collection links have no article title: their text is stored as label.
    NULL fields and explicit availability avoid inventing citing articles.
    """
    result = {key: {'status': 'unavailable', 'items': []} for key in (
        'detail_cited_by', 'detail_related_articles', 'detail_all_versions')}
    for field in soup.select('.gsc_oci_field'):
        key = RELATION_FIELDS.get(_norm_label(field.get_text()))
        if key:
            # A displayed relation field without a usable reference is distinct
            # from a field absent from the source altogether.
            result[key]['status'] = 'extraction_error'
    for anchor in soup.select('a'):
        if anchor.find_parent(id='gsc_oci_graph_wrapper') is not None:
            continue
        href = anchor.get('href') or ''
        try:
            query = parse_qs(urlsplit(href).query)
        except ValueError:
            href, query = '', {}
        label = anchor.get_text(' ', strip=True)
        normalized = _norm_label(label)
        related_query = query.get('q', [''])[0]
        identifier = query.get('cites', query.get('cluster', [None]))[0]
        if identifier is None and related_query.startswith('related:'):
            identifier = related_query
        block = anchor.find_parent(class_='gs_scl')
        field = block.select_one('.gsc_oci_field') if block else None
        field_label = _norm_label(field.get_text()) if field else ''
        key = RELATION_FIELDS.get(field_label)
        navigation_label = (
            normalized in RELATION_FIELDS
            or re.match(r'^(cited by|dirujuk|dikutip|all \d+ versions|semua \d+ versi)', normalized)
        )
        article_title = (label or None) if key and not navigation_label else None
        # Explicit relation context describes the link's role. A cluster URL
        # can identify an article in a Related articles block too.
        if key is None:
            if 'cites' in query or re.match(r'^(cited by|dirujuk|dikutip)', normalized):
                key = 'detail_cited_by'
            elif related_query.startswith('related:') or normalized in ('related articles', 'artikel terkait'):
                key = 'detail_related_articles'
            elif 'cluster' in query or re.match(r'^(all \d+ versions|semua \d+ versi)', normalized):
                key = 'detail_all_versions'
        if key is None:
            continue
        try:
            url = urljoin(base_url, href) if href else None
            if url and urlsplit(url).scheme not in ('http', 'https'):
                url = None
        except ValueError:
            url = None
        if not url and not identifier:
            if not result[key]['items']:
                result[key]['status'] = 'extraction_error'
            continue
        item = {'title': article_title,
                'label': label or None, 'url': url, 'identifier': identifier}
        # No title inferred from navigation labels. Bound work to this page.
        if item not in result[key]['items'] and len(result[key]['items']) < 100:
            result[key]['items'].append(item)
        result[key]['status'] = 'available'
    return result

# Label yang ikut menentukan kategori tipe venue (detail_venue_label
# menyimpan teks label ASLI). "venue" sengaja TIDAK di sini: itu nama
# venue, bukan tipe.
LABEL_VENUE_TIPE = ("jurnal", "konferensi", "journal", "conference")

# Alias fallback deskripsi (jalur fallback setelah #gsc_oci_descr) -
# TIDAK masuk pemetaan supaya tidak pernah menimpa jalur utama.
LABEL_DESKRIPSI_FALLBACK = (
    "deskripsi", "abstract", "abstrak", "description", "ringkasan",
)

# Label (bentuk ternormalisasi) -> key hasil; dibalik dari LABEL_DETAIL
# sehingga tidak ada dua sumber kebenaran.
_PEMETAAN_LABEL = {
    label: kunci
    for kunci, daftar in LABEL_DETAIL.items()
    for label in daftar
}


def _norm_label(teks):
    """Normalisasi label PERBANDINGAN: kapitalisasi + spasi.

    " ".join(split()) menangani spasi luar/interior, tab/newline, dan
    NBSP (\xa0 termasuk whitespace Python) lalu lowercase. Hanya untuk
    pencocokan - teks label asli (bila disimpan) tidak dimodifikasi.
    Tidak ada normalisasi lain (tanda baca tidak disentuh).
    """
    return " ".join(teks.split()).lower()


def normalisasi_judul(teks):
    """Normalisasi judul UNTUK PERBANDINGAN saja (bukan penyimpanan).

    Lowercase, NBSP/whitespace dirapikan, tanda baca diganti spasi.
    Dua judul dianggap sama hanya bila hasil normalisasinya PERSIS
    sama (exact match setelah normalisasi) - TIDAK ada pencocokan
    fuzzy. Nilai judul asli yang disimpan di baris TIDAK pernah
    diubah oleh fungsi ini.
    """
    if not isinstance(teks, str):
        return ""
    rendah = teks.replace("\xa0", " ").lower()
    tanpa_tanda = re.sub(r"[^\w\s]", " ", rendah)
    return re.sub(r"\s+", " ", tanpa_tanda).strip()


def parse_detail_html(html, base_url='https://scholar.google.com'):
    """Parse halaman detail publikasi menjadi dict field detail.

    Field diambil dari pasangan div.gs_scl (label div.gsc_oci_field +
    nilai div.gsc_oci_value) plus elemen khusus judul/link. Pencocokan
    label PERSIS setelah normalisasi _norm_label (kapitalisasi, spasi
    luar/interior, NBSP) - TANPA substring matching. Field yang tidak
    ada di halaman -> None; value div kosong juga -> None.

    Label didukung (LABEL_DETAIL), dua bahasa:
      - Indonesia - TERBUKTI di fixture hl=id: Pengarang, Tanggal
        terbit, Jurnal, Konferensi, Jilid, Terbitan, Halaman, Penerbit,
        Total kutipan, Deskripsi.
      - Inggris - alias NON-FIXTURE (belum diverifikasi terhadap HTML
        live Scholar): penulis/author(s), publication date/date,
        journal/conference/venue, volume/vol, issue/nomor,
        pages/page/page range, publisher, cited by.

    Venue bertipe eksplisit dipisah ke detail_journal/conference/book;
    detail_venue tetap menyimpan sumber asli. Jenis tanpa bukti tetap
    unknown. Number dan Series tidak dipakai sebagai bukti tipe.
    Relasi hanya dikumpulkan dari halaman ini (maksimal 100 per jenis),
    tanpa membuka URL tujuan; base_url mempertahankan domain Scholar.

    KECUALI `detail_citations`: nilainya diambil dari struktur DOM lewat
    _ekstrak_sitasi_dari_blok (angka pada anchor 'cites=' di luar graph
    wrapper), bukan gabungan seluruh teks blok - baik label "Total
    kutipan" maupun alias "cited by". Tetap str|None (mis. "3").

    `detail_description` berjalur utama selector #gsc_oci_descr; bila
    selector itu tidak menemukan apa pun (masih None), dipakai FALLBACK
    berupa blok berlabel persis salah satu LABEL_DESKRIPSI_FALLBACK
    (deskripsi/abstract/abstrak/description/ringkasan; struktur
    div.gs_scl -> field + value sama seperti label lain). Fallback
    hanya mengambil value div (label tidak ikut), value kosong
    diabaikan, blok pertama yang cocok menang, dan label-label itu
    TIDAK dimasukkan ke pemetaan (tidak menimpa jalur utama).
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")

    def _teks(selector):
        el = soup.select_one(selector)
        return (el.get_text(' ', strip=True) or None) if el else None

    hasil = {
        "detail_title": _teks("#gsc_oci_title"),
        "detail_authors": None,
        "detail_date": None,
        "detail_venue": None,
        "detail_venue_label": None,
        "detail_volume": None,
        "detail_issue": None,
        "detail_pages": None,
        "detail_publisher": None,
        "detail_description": _teks("#gsc_oci_descr"),
        "detail_citations": None,
        "detail_link": None,
        "detail_pdf_link": None,
        "detail_doi": None,
    }
    hasil = {**_detail_kosong(), **hasil}
    recognized = bool(soup.select_one('#gsc_oci_title, .gsc_oci_field'))
    hasil['detail_extraction_status'] = 'parsed' if recognized else 'unrecognized_html'
    if recognized:
        hasil['detail_publication_type'] = 'unknown'
        hasil.update(_parse_relations(soup, base_url))
    publication_types = set()

    link = soup.select_one("a.gsc_oci_title_link")
    if link is not None:
        hasil["detail_link"] = link.get("href")

    pdf = soup.select_one("div.gsc_oci_title_ggi a")
    if pdf is not None:
        hasil["detail_pdf_link"] = pdf.get("href")

    # Label -> kunci hasil lewat _PEMETAAN_LABEL (dibangun dari
    # LABEL_DETAIL; lihat docstring utk alias & penolakan). Label venue-
    # tipe (LABEL_VENUE_TIPE) menyimpan teks label ASLI di
    # detail_venue_label sebagai pembeda tipe utk turunan kategori.
    for label in soup.select(".gsc_oci_field"):
        # Pair only with this field's sibling, never a descendant of some
        # unrelated row when the expected value element is missing.
        nilai = label.find_next_sibling()
        if nilai is None or 'gsc_oci_value' not in nilai.get('class', []):
            continue
        teks_label = _norm_label(label.get_text())
        value = nilai.get_text(' ', strip=True)
        venue_field = VENUE_FIELDS.get(teks_label)
        if teks_label in ('publication type', 'jenis publikasi'):
            explicit_type = _norm_label(value)
            if explicit_type in VENUE_FIELDS:
                publication_types.add(VENUE_FIELDS[explicit_type][1])
            elif explicit_type in ('doctoral dissertation', 'masters thesis', "master's thesis"):
                publication_types.add('thesis')
        if venue_field and value:
            column, publication_type = venue_field
            hasil[column] = value
            publication_types.add(publication_type)
            if hasil['detail_venue'] is None:
                hasil['detail_venue'] = value
                hasil['detail_venue_label'] = label.get_text(strip=True)
        kunci = _PEMETAAN_LABEL.get(teks_label)
        if kunci is not None:
            if kunci == "detail_citations":
                # Sitasi: angka dari anchor 'cites=' di luar graph
                # wrapper (bukan gabungan teks blok seperti tahun/bar).
                hasil[kunci] = _ekstrak_sitasi_dari_blok(nilai)
            else:
                teks_nilai = nilai.get_text(' ', strip=True)
                if teks_nilai:  # value kosong -> tetap None
                    hasil[kunci] = teks_nilai
            if teks_label in LABEL_VENUE_TIPE:
                hasil["detail_venue_label"] = label.get_text(strip=True)

    # Fallback deskripsi: jalur utama (#gsc_oci_descr, saat inisialisasi)
    # bisa tidak ada di halaman tertentu. Bila hasilnya masih None, ambil
    # dari blok berlabel persis salah satu LABEL_DESKRIPSI_FALLBACK -
    # struktur yang sama seperti label pemetaan lain (div.gs_scl ->
    # field + value). Hanya value div yang dipakai (label tidak ikut),
    # value kosong diabaikan, dan blok pertama yang cocok menang.
    # Label-label ini TIDAK dimasukkan ke pemetaan supaya tidak
    # menimpa jalur utama.
    if hasil["detail_description"] is None:
        for blok in soup.select(".gs_scl"):
            label = blok.select_one(".gsc_oci_field")
            if label is None:
                continue
            if _norm_label(label.get_text()) not in LABEL_DESKRIPSI_FALLBACK:
                continue
            nilai = blok.select_one(".gsc_oci_value")
            if nilai is None:
                continue
            teks = nilai.get_text(' ', strip=True)
            if teks:
                hasil["detail_description"] = teks
                break

    if publication_types:
        if publication_types <= {'book', 'book_chapter'}:
            hasil['detail_publication_type'] = 'book_chapter' if 'book_chapter' in publication_types else 'book'
        else:
            hasil['detail_publication_type'] = next(iter(publication_types)) if len(publication_types) == 1 else 'unknown'
    explicit_doi = _POLA_DOI.search(hasil['detail_doi'] or '')
    doi, konflik = _ekstrak_doi(hasil["detail_link"], hasil["detail_pdf_link"])
    hasil["detail_doi"] = explicit_doi.group(0) if explicit_doi else doi
    if konflik is not None and not explicit_doi:
        # DOI berbeda di dua URL -> simpan NULL (jangan pilih diam-diam),
        # catat kedua URL sumber dan kedua DOI hasil parsing untuk
        # pemeriksaan manual. Ikut pola print/log proyek (tanpa kolom DB).
        print(
            "    doi ambigu -> detail_doi=NULL"
            f" (detail_link={hasil['detail_link']},"
            f" detail_pdf_link={hasil['detail_pdf_link']};"
            f" detail_link_doi={konflik['detail_link_doi']},"
            f" detail_pdf_doi={konflik['detail_pdf_doi']})"
        )

    return hasil


def detect_block(html):
    """Deteksi halaman blokir / captcha Google Scholar."""
    lowered = html.lower()
    markers = [
        "unusual traffic",
        "not a robot",
        "id=\"captcha\"",
        "sorry/index",
        "detected unusual",
    ]
    return any(m in lowered for m in markers)


def _host_google(host):
    """True bila `host` milik Google (google.com + varian regional).

    String "google" HARUS berdiri sebagai label domain sendiri
    ("notgoogle.com" -> False), dan setelahnya maksimal dua label
    suffix TLD/gTLD ("com", "de", "co.id", "com.au", ...).
    """
    labels = [l for l in host.lower().split(".") if l]
    if "google" not in labels:
        return False
    i = labels.index("google")
    return len(labels) - i - 1 <= 2


def _url_blokir(url):
    """True bila URL final mengarah ke halaman blokir Google (/sorry/).

    Deteksi berbasis URL (bukan HTML): varian sorry-page pada run 160
    tidak memuat marker yang dikenali detect_block(), tapi URL
    final-nya jelas google.com/sorry/index. Host divalidasi via
    _host_google supaya host lain dengan path serupa tidak salah
    dikenali.
    """
    if not url:
        return False
    try:
        bagian = urlsplit(url)
    except ValueError:
        return False
    return _host_google(bagian.hostname or "") and (
        bagian.path or "").startswith("/sorry/")


def human(seconds):
    return f"{seconds:.2f} detik"


async def _ambil_detail_publikasi(page, all_rows, verbose=False):
    """Fase halaman detail: buka tiap baris & gabungkan field detail.

    Mutasi `all_rows` di tempat. Mengembalikan status yang diproses di
    DALAM fungsi:

        (partial_baru, error_message_baru, blocked_baru, failures_baru,
         rate_limited_baru)

    Pemanggil (`scrape_scholar`) menggabungkannya dengan semantik
    PERTAHANAN: `partial = partial or ...`, `error_message = error_message
    or ...` (first-error-wins tetap berlaku, pesan lama tidak pernah
    ditimpa), `blocked = blocked or ...`, `rate_limited = rate_limited
    or ...`, `failures.extend(...)`.

    INVARIAN STRUKTUR: sebelum loop per-row, SEMUA row di `all_rows`
    di-pre-fill dengan `_detail_kosong()` -> setiap row yang keluar dari
    fase ini selalu punya lengkapnya key DETAIL_KEYS (None) apa pun
    jalur keluarnya; jalur sukses menimpa dengan hasil parse. Key profil
    tidak pernah disentuh.

    Entri `failures_baru` (format aditif: index, raw_title, reason,
    detail_url - JSON-safe, hanya str untuk pesan exception):
      - detail_url_tidak_tersedia - row tanpa detail_url (dulu senyap).
        `partial` TIDAK diset untuk kasus ini: kondisi data profil, bukan
        kegagalan proses (keputusan sadar).
      - detail_fetch_error - goto exception, HTTP >=400, atau
        wait_for_selector timeout non-block. KASUS KHUSUS HTTP 429
        (rate limit): entri tetap dicatat untuk row yang sedang
        diproses, lalu loop DIHENTIKAN (break) tanpa retry otomatis -
        permintaan detail berikutnya tidak dikirim; `blocked` tetap
        False (429 bukan captcha); row sesudahnya belum dicoba
        (struktur None dari pre-fill). `rate_limited_baru` diset True:
        lewat elemen kelima ini run diklasifikasi RATE_LIMITED (bukan
        PARTIAL_SUCCESS) - sinyal first-class supaya pemanggil batch
        menghentikan seluruh batch; baris yang sudah berhasil tetap
        dipertahankan di envelope (hasil parsial tetap bisa disimpan).
      - detail_parse_error - parse_detail_html melempar.
      - detail_blocked - break karena block terdeteksi; entri hanya untuk
        row yang SEDANG diproses; row sesudahnya belum dicoba (struktur
        None dari pre-fill). partial=True, blocked=True dipertahankan.
        Pemicu: marker HTML captcha (detect_block) ATAU redirect URL ke
        halaman blokir Google (sorry-page, _url_blokir).

    Urutan langkah per row dipertahankan: jitter -> goto -> cek
    status -> tunggu selector -> content -> deteksi block -> parse
    (tanpa jitter di akhir iterasi; jeda acak diberikan SEBELUM tiap
    navigasi detail, termasuk request detail pertama). Print verbose
    mengikuti pola proyek (print, bukan logging).
    """
    partial_baru = False
    error_message_baru = None
    blocked_baru = False
    failures_baru = []
    rate_limited_baru = False

    # Invarian struktur: semua row punya key detail_* (None) lebih dulu.
    for row in all_rows:
        row.update(_detail_kosong())

    for i, row in enumerate(all_rows):
        detail_url = row.get("detail_url")
        if not detail_url:
            # Kondisi data profil -> diamati lewat failures, tapi TIDAK
            # menyet partial (lihat docstring).
            failures_baru.append({
                "index": i,
                "raw_title": row.get("title"),
                "reason": "detail_url_tidak_tersedia",
                "detail_url": None,
            })
            continue
        # jeda acak sebelum tiap navigasi detail supaya traffic tidak
        # terbaca bot (termasuk request detail pertama).
        await page.wait_for_timeout(
            random.randint(
                int(JITTER_DETAIL_MIN * 1000),
                int(JITTER_DETAIL_MAX * 1000),
            )
        )
        try:
            resp = await page.goto(
                detail_url, wait_until="domcontentloaded",
                timeout=TIMEOUT_MS,
            )
        except Exception as exc:
            print(f"    detail {i + 1} GAGAL buka: {exc}")
            partial_baru = True
            error_message_baru = (
                error_message_baru or f"detail gagal dibuka: {exc}"
            )
            failures_baru.append({
                "index": i,
                "raw_title": row.get("title"),
                "reason": "detail_fetch_error",
                "detail_url": detail_url,
            })
            continue

        if resp is not None and resp.status >= 400:
            ke_429 = resp.status == 429
            print(f"    detail {i + 1} HTTP {resp.status}")
            partial_baru = True
            error_message_baru = (
                error_message_baru
                or (
                    # 429 = rate limit: pesan menyebut penghentian supaya
                    # jelas run berhenti karena pembatasan, bukan error umum.
                    "detail HTTP 429 — rate limit, "
                    "pengambilan detail dihentikan"
                    if ke_429
                    else f"detail HTTP {resp.status}"
                )
            )
            failures_baru.append({
                "index": i,
                "raw_title": row.get("title"),
                "reason": "detail_fetch_error",
                "detail_url": detail_url,
            })
            if ke_429:
                # Hormati pembatasan Google Scholar: HENTIKAN loop, jangan
                # kirim permintaan detail berikutnya, tanpa retry otomatis.
                # Baris sesudah ini tidak dicoba; semua row tetap di
                # all_rows dengan key detail None (pre-fill) dan metadata
                # profil utuh. rate_limited_baru=True -> run diklasifikasi
                # RATE_LIMITED (bukan PARTIAL_SUCCESS): sinyal first-class
                # supaya pemanggil batch menghentikan seluruh batch.
                rate_limited_baru = True
                print("    HTTP 429 — hentikan pengambilan detail.")
                break
            continue

        # Halaman blokir Google (sorry-page): redirect terjadi saat goto,
        # umumnya dengan status 200 sehingga cabang status di atas tidak
        # terpicu (bukti: run 160, audit P45). HENTIKAN fase detail tanpa
        # retry - sama seperti blok captcha - dan pertahankan semua data
        # yang sudah ada. JANGAN menyebut HTTP 429: status respons tidak
        # diamati di jalur ini.
        if _url_blokir(page.url):
            print(f"    detail {i + 1}: terredirect ke halaman blokir (sorry).")
            partial_baru = True
            error_message_baru = (
                error_message_baru
                or "detail terredirect ke halaman blokir Google "
                    "(sorry-page)"
            )
            failures_baru.append({
                "index": i,
                "raw_title": row.get("title"),
                "reason": "detail_blocked",
                "detail_url": detail_url,
            })
            blocked_baru = True
            break

        try:
            await page.wait_for_selector(
                "#gsc_oci_title", timeout=TIMEOUT_MS
            )
        except Exception as exc:
            html = await page.content()
            kaptcha = detect_block(html)
            if kaptcha or _url_blokir(page.url):
                print("  DIBLOKIR Google saat ambil detail.")
                blocked_baru = True
                partial_baru = True
                error_message_baru = (
                    error_message_baru
                    or (
                        "captcha / unusual traffic saat detail"
                        if kaptcha
                        else "detail terredirect ke halaman blokir Google "
                             "(sorry-page)"
                    )
                )
                failures_baru.append({
                    "index": i,
                    "raw_title": row.get("title"),
                    "reason": "detail_blocked",
                    "detail_url": detail_url,
                })
                break
            print(f"    detail {i + 1} tidak termuat: {exc}")
            partial_baru = True
            error_message_baru = (
                error_message_baru or f"detail tidak termuat: {exc}"
            )
            failures_baru.append({
                "index": i,
                "raw_title": row.get("title"),
                "reason": "detail_fetch_error",
                "detail_url": detail_url,
            })
            continue

        html = await page.content()
        if detect_block(html):
            print("  DIBLOKIR Google saat ambil detail.")
            blocked_baru = True
            partial_baru = True
            error_message_baru = (
                error_message_baru or "captcha / unusual traffic saat detail"
            )
            failures_baru.append({
                "index": i,
                "raw_title": row.get("title"),
                "reason": "detail_blocked",
                "detail_url": detail_url,
            })
            break

        try:
            detail = parse_detail_html(html, base_url=detail_url)
        except Exception as exc:
            print(f"    detail {i + 1} parse gagal: {exc}")
            partial_baru = True
            error_message_baru = (
                error_message_baru or f"detail parse gagal: {exc}"
            )
            failures_baru.append({
                "index": i,
                "raw_title": row.get("title"),
                "reason": "detail_parse_error",
                "detail_url": detail_url,
            })
            continue

        row.update(detail)
        if verbose:
            print(f"    detail {i + 1}/{len(all_rows)}: OK")

    return (partial_baru, error_message_baru, blocked_baru, failures_baru,
            rate_limited_baru)


# ============================================================
# SCRAPER
# ============================================================

async def scrape_scholar(uid, headless=True, verbose=True, max_batches=MAX_BATCHES,
                         ambil_detail=True,
                         judul_sudah_tersimpan: "set | None" = None):
    """Scrape seluruh publikasi dari satu Google Scholar user ID.

    CATATAN PENTING SOAL PAGING
    ----------------------------
    Parameter cstart & pagesize di URL profil Google Scholar TIDAK bisa
    dipakai untuk paging. Diuji langsung ke server:

        cstart=0&pagesize=100  -> 10 baris
        cstart=10&pagesize=100 ->  0 baris  (data yang sama, diulang)
        cstart=20&pagesize=100 ->  0 baris

    Google Scholar memaksa halaman profil selalustarting dari baris
    pertama, apa pun nilai cstart. Cara yang benar adalah menekan
    tombol "Tampilkan lainnya" (#gsc_bpf_more) berulang kali. Tombol
    ini otomatis becomes disabled (= attribute "disabled") saat semua
    publikasi sudah termuat.

    FASE HALAMAN DETAIL
    -------------------
    Setelah paging daftar selesai, tiap publikasi dibuka satu per satu
    untuk mengambil field detail (pengarang, tanggal, venue, jilid,
    halaman, penerbit, dsb.) yang digabung ke baris `rows`. Fase ini
    default ON; `ambil_detail=False` melewatinya. Kegagalan per-publikasi
    dicatat ke `failures` dan tidak menggagalkan run. Block saat fase
    detail menghentikan fase itu tapi data daftar tetap disimpan ->
    PARTIAL_SUCCESS. HTTP 429 (rate limit) juga menghentikan fase itu
    tanpa retry - permintaan detail berikutnya tidak dikirim - dan data
    yang sudah terkumpul tetap disimpan, TAPI statusnya RATE_LIMITED
    (bukan PARTIAL_SUCCESS): 429 adalah sinyal first-class supaya
    pemanggil batch menghentikan SELURUH batch. Redirect ke halaman
    blokir Google (sorry-page) juga menghentikan fase itu tanpa retry
    (reason detail_blocked), tanpa menyebut HTTP 429 karena status
    respons tidak diamati di jalur itu.

    FASE DAFTAR & SORRY-PAGE
    ------------------------
    Fase daftar mengenali halaman blokir Google lewat dua sinyal:
    marker HTML (detect_block) ATAU redirect URL final ke /sorry/ pada
    host Google (_url_blokir). Sorry-page saat navigasi awal -> BLOCKED
    tanpa data; sorry-page saat paging -> PARTIAL_SUCCESS dengan data
    batch sebelumnya dipertahankan.

    HTTP 429 = RATE_LIMITED (bukan TIMEOUT, bukan PARTIAL_SUCCESS):
      - navigasi awal (goto profil) -> fatal tanpa data, pesan
        "HTTP 429 dari server" dipertahankan;
      - saat paging -> terdeteksi lewat listener event "response"
        (klik tidak mengembalikan respons, jadi subrequest 429 adalah
        satu-satunya sinyal); loop paging langsung berhenti, batch
        terakhir TIDAK di-parse, data batch sebelumnya dipertahankan;
      - fase detail -> loop detail berhenti tanpa retry (lihat atas).
    Pada semua jalur: tanpa retry, tanpa ganti domain, tanpa bypass -
    pemanggil batch wajib menghentikan seluruh batch.

    MODE INCREMENTAL
    ----------------
    `judul_sudah_tersimpan` (opsional) berisi himpunan judul yang sudah
    TERNORMALISASI lewat normalisasi_judul() - berasal dari pemanggil
    (mis. judul publikasi yang sudah tersimpan di RAW PostgreSQL).
    None (default) = scraping awal penuh, perilaku tidak berubah.

    Bila diisi (mode incremental): fase daftar + paging tetap
    menjelajah SEMUA halaman (deteksi blokir/sorry, dedupe
    seen_titles, dan logika "paging tidak maju" tidak berubah), lalu
    baris yang judulnya - setelah normalisasi, exact match, TANPA
    fuzzy - sudah ada di himpunan itu dibuang SEBELUM fase detail,
    sehingga publikasi lama tidak pernah dinavigasi ulang. Teks judul
    asli di baris tidak pernah diubah (normalisasi hanya untuk
    perbandingan). Envelope hanya berisi baris BARU: records_fetched
    dan data.rows = baris baru saja. Profil punya publikasi tapi nol
    yang baru -> SUCCESS (bukan NO_DATA); kegagalan detail pada baris
    baru tetap menghasilkan status sesuai penyebabnya (HTTP 429 ->
    RATE_LIMITED dengan baris baru dipertahankan; gagal lain ->
    PARTIAL_SUCCESS), dan blokir/network error saat fase daftar tetap
    diklasifikasi normal (BLOCKED/TIMEOUT).

    Return: run envelope 9 kunci berisi run_id, source, started_at,
    finished_at, status, records_fetched, records_failed,
    error_message, dan data. `data` berisi scholar_id, name, h_index,
    rows, failures; `data` bernilai None HANYA pada run fatal (tanpa
    data sama sekali).
    """
    from playwright.async_api import async_playwright

    run_id = str(uuid.uuid4())
    started_at = _utc_now_iso()

    all_rows = []
    failures = []
    seen_titles = set()
    name = None
    h_index = None
    batches = 0
    used_domain = DOMAINS[0]
    t_start = time.perf_counter()

    # penanda untuk pohon keputusan status
    blocked = False
    network_error = False
    parse_error = False
    expected_empty = False
    partial = False
    # HTTP 429 teramati (navigasi awal / paging / detail) -> RATE_LIMITED
    rate_limited = False
    error_message = None
    parsed_total = 0
    # mode incremental: profil punya publikasi tapi semuanya sudah
    # tersimpan (dihitung setelah filter, dibaca _envelope)
    tanpa_publikasi_baru = False

    def _envelope():
        """Bangun run envelope dari state saat ini."""
        has_data = bool(all_rows)
        if (tanpa_publikasi_baru and not blocked and not network_error
                and not parse_error and not rate_limited):
            # Mode incremental: profil punya publikasi tapi semuanya sudah
            # tersimpan di RAW -> bukan NO_DATA, ini pemeriksaan yang sukses
            # tanpa publikasi baru. (rate_limited ikut dikecualikan supaya
            # 429 selalu menang sebagai RATE_LIMITED.)
            status = "SUCCESS"
        else:
            status = _classify_status(
                has_data=has_data,
                blocked=blocked,
                network_error=network_error,
                parse_error=parse_error,
                expected_empty=expected_empty,
                partial=partial,
                rate_limited=rate_limited,
            )
        data = _make_data(uid, name, h_index, all_rows, failures,
                          used_domain, batches,
                          time.perf_counter() - t_start) if has_data else None
        return _make_envelope(
            run_id=run_id,
            started_at=started_at,
            status=status,
            records_fetched=len(all_rows),
            # run fatal tidak pernah menghitung publikasi -> 0
            records_failed=len(failures) if has_data else 0,
            error_message=error_message,
            data=data,
        )

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=headless,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        try:
            context = await browser.new_context(
                user_agent=USER_AGENT,
                viewport={"width": 1440, "height": 900},
                locale="id-ID",
            )
            page = await context.new_page()

            # Listener respons: deteksi 429 saat paging. Klik "Tampilkan
            # lainnya" tidak mengembalikan objek respons, jadi satu-satunya
            # sinyal 429 di fase paging adalah event "response" dari
            # subrequest yang dipicu klik. Listener ini dikosultkan HANYA
            # di loop paging; dua goto lain (profil & detail) memakai cek
            # resp.status eksplisit. Daftar dibersihkan tepat sebelum tiap
            # klik supaya 429 basi (subresource saat muat awal) tidak
            # menghentikan klik berikutnya secara keliru.
            respons_429 = []

            def _catat_resp_429(resp):
                if resp.status == 429:
                    respons_429.append(resp.url)

            page.on("response", _catat_resp_429)

            url = build_url(uid, start=0, domain=used_domain)
            if verbose:
                print(f"  buka: {used_domain}")
                print(f"  URL : {url}")

            try:
                resp = await page.goto(
                    url, wait_until="domcontentloaded", timeout=TIMEOUT_MS
                )
            except Exception as exc:
                print(f"  GAGAL buka URL: {exc}")
                network_error = True
                error_message = f"gagal buka URL: {exc}"
                await browser.close()
                return _envelope()

            if resp is not None and resp.status >= 400:
                print(f"  HTTP {resp.status} dari server, hentikan.")
                network_error = True
                if resp.status == 429:
                    # 429 = rate limit, bukan timeout biasa; status run
                    # RATE_LIMITED dan pemanggil batch wajib menghentikan
                    # seluruh batch (tanpa retry / ganti domain).
                    rate_limited = True
                error_message = f"HTTP {resp.status} dari server"
                await browser.close()
                return _envelope()

            # Halaman blokir Google (sorry-page): redirect terjadi saat
            # goto, umumnya dengan status 200 sehingga cek status di atas
            # tidak terpicu (audit P48; pola nyata run 160). Tanpa data,
            # tanpa retry - sama seperti captcha - dan JANGAN menyebut
            # HTTP 429 karena status respons tidak diamati di jalur ini.
            if _url_blokir(page.url):
                print("  DIBLOKIR Google (redirect sorry-page).")
                blocked = True
                error_message = (
                    "halaman daftar terredirect ke halaman blokir "
                    "Google (sorry-page)"
                )
                await browser.close()
                return _envelope()

            # tunggu tabel publikasi muncul
            try:
                await page.wait_for_selector("#gsc_a_b", timeout=TIMEOUT_MS)
            except Exception as exc:
                html = await page.content()
                kaptcha = detect_block(html)
                if kaptcha or _url_blokir(page.url):
                    print("  DIBLOKIR Google (captcha / unusual traffic).")
                    print("  Coba: --headed, ganti SCHOLAR_ID, atau pakai proxy.")
                    blocked = True
                    error_message = (
                        "captcha / unusual traffic"
                        if kaptcha
                        else "halaman daftar terredirect ke halaman "
                             "blokir Google (sorry-page)"
                    )
                else:
                    print("  Tabel #gsc_a_b tidak muncul. Hentikan.")
                    network_error = True
                    error_message = f"tabel #gsc_a_b tidak muncul: {exc}"
                await browser.close()
                return _envelope()

            html = await page.content()
            if detect_block(html):
                print("  DIBLOKIR Google (captcha / unusual traffic).")
                blocked = True
                error_message = "captcha / unusual traffic"
                await browser.close()
                return _envelope()

            # ---------- batch pertama ----------
            batch_start = parsed_total
            try:
                name, rows, h_index = parse_profile_html(html)
            except Exception as exc:
                print(f"    parse batch 1 gagal: {exc}")
                parse_error = True
                partial = True
                error_message = f"gagal parse batch 1: {exc}"
                failures.append({
                    "index": parsed_total,
                    "raw_title": None,
                    "reason": "batch_parse_error",
                })
                rows = []
            parsed_total += len(rows)
            valid = _collect_rows(rows, failures, batch_start)
            all_rows.extend(valid)
            for r in valid:
                seen_titles.add(r["title"])
            batches = 1

            if verbose:
                print(f"    batch 1: {len(valid)} baris")

            # ---------- tekan "Tampilkan lainnya" sampai habis ----------
            while batches < max_batches:
                more = page.locator("#gsc_bpf_more")

                try:
                    if not await more.is_visible():
                        if verbose:
                            print("    tombol 'Tampilkan lainnya' tidak ada. Selesai.")
                        break
                except Exception as exc:
                    print(f"    cek tombol gagal: {exc}")
                    partial = True
                    error_message = error_message or f"cek tombol gagal: {exc}"
                    break

                # tombol disabled = tidak ada publikasi lagi
                if await more.get_attribute("disabled") is not None:
                    if verbose:
                        print("    tombol nonaktif, semua publikasi sudah termuat.")
                    break

                # jeda acak sebelum klik supaya traffic tidak terbaca bot
                # (hanya saat klik benar-benar akan dilakukan: cek
                # visible/disabled di atas sudah lolos).
                await page.wait_for_timeout(
                    random.randint(int(JITTER_MIN * 1000), int(JITTER_MAX * 1000))
                )

                before = len(all_rows)
                # bersihkan catatan 429 basi: hanya respons yang datang
                # SELAMA klik ini yang boleh menghentikan paging.
                respons_429.clear()
                try:
                    await more.click()
                except Exception as exc:
                    print(f"    klik gagal: {exc}")
                    partial = True
                    error_message = error_message or f"klik gagal: {exc}"
                    break

                html = await page.content()
                if respons_429:
                    # 429 teramati pada subrequest paging: hentikan
                    # pengambilan daftar. Batch ini TIDAK di-parse, tidak
                    # ada klik lanjutan, dan fase detail tidak dijalankan
                    # (guard rate_limited di bawah); data batch
                    # sebelumnya tetap di all_rows.
                    print("  HTTP 429 terdeteksi saat paging — hentikan pengambilan daftar.")
                    rate_limited = True
                    partial = True
                    error_message = error_message or "HTTP 429 saat paging — pengambilan daftar dihentikan"
                    break
                kaptcha = detect_block(html)
                if kaptcha or _url_blokir(page.url):
                    print("  DIBLOKIR Google setelah beberapa batch.")
                    blocked = True
                    partial = True
                    error_message = error_message or (
                        "captcha / unusual traffic saat paging"
                        if kaptcha
                        else "halaman daftar terredirect ke halaman "
                             "blokir Google (sorry-page) saat paging"
                    )
                    break

                batch_start = parsed_total
                try:
                    _, new_rows, _ = parse_profile_html(html)
                except Exception as exc:
                    print(f"    parse batch {batches + 1} gagal: {exc}")
                    parse_error = True
                    partial = True
                    error_message = error_message or f"gagal parse batch {batches + 1}: {exc}"
                    failures.append({
                        "index": parsed_total,
                        "raw_title": None,
                        "reason": "batch_parse_error",
                    })
                    batches += 1
                    continue
                parsed_total += len(new_rows)
                valid_new = _collect_rows(new_rows, failures, batch_start)

                # hanya ambil yang benar-benar baru ( jagaan duplikat )
                fresh = [r for r in valid_new if r["title"] not in seen_titles]
                for r in fresh:
                    seen_titles.add(r["title"])
                all_rows.extend(fresh)
                batches += 1

                if verbose:
                    print(f"    batch {batches}: +{len(fresh)} baris "
                          f"(total {len(all_rows)})")

                # tidak ada baris baru = paging tidak maju, berhenti
                if len(all_rows) == before:
                    if verbose:
                        print("    tidak ada baris baru, berhenti.")
                    partial = True
                    error_message = error_message or "paging tidak maju"
                    break

            # ---------- Safety: batas batch tercapai ----------
            if batches >= max_batches:
                partial = True
                error_message = error_message or f"mencapai batas {max_batches} batch"
                if verbose and len(all_rows):
                    print(f"   [!] mencapai batas {max_batches} batch, "
                          f"hasil mungkin belum lengkap.")

            # ---------- absolutisasi URL detail ----------
            # href dari halaman daftar relatif ("/citations?...");
            # jadikan URL penuh supaya bisa dipakai navigasi & disimpan
            # mentah di raw.
            for r in all_rows:
                url = r.get("detail_url")
                if url and not url.startswith("http"):
                    r["detail_url"] = f"https://{used_domain}{url}"

            # ---------- filter incremental (mode lanjutan) ----------
            # judul_sudah_tersimpan = himpunan judul TERNORMALISASI
            # (normalisasi_judul) milik pemanggil (mis. yang sudah
            # tersimpan di RAW). Fase daftar + paging di atas tetap
            # menjelajah SEMUA halaman; filter ini hanya memotong baris
            # lama SEBELUM fase detail supaya publikasi lama tidak
            # pernah dinavigasi ulang. Exact match setelah normalisasi,
            # tanpa fuzzy; teks judul asli di baris tidak diubah.
            incremental = judul_sudah_tersimpan is not None
            tanpa_publikasi_baru = False
            if judul_sudah_tersimpan is not None:
                terlihat = len(all_rows)
                all_rows = [
                    r for r in all_rows
                    if normalisasi_judul(r.get("title") or "") not in judul_sudah_tersimpan
                ]
                tanpa_publikasi_baru = incremental and terlihat > 0 and not all_rows
                if verbose:
                    print(f"  incremental: {terlihat} publikasi terlihat, "
                          f"{len(all_rows)} baru")

            # ---------- fase halaman detail per publikasi ----------
            # Default ON. Kegagalan per-publikasi dicatat ke failures
            # (data daftar tetap dipakai) -> PARTIAL_SUCCESS. Block
            # menghentikan fase ini tapi data daftar tetap disimpan.
            # rate_limited (429) juga menghentikan fase ini: setelah
            # rate limit tidak ada permintaan lagi yang dikirim.
            # Badan fase diekstrak ke _ambil_detail_publikasi() supaya
            # bisa diuji tanpa browser; statusnya digabung di sini dengan
            # semantik PERTAHANAN (nilai lama tidak pernah ditimpa).
            if ambil_detail and all_rows and not blocked and not rate_limited:
                if verbose:
                    print(f"  ambil halaman detail: {len(all_rows)} publikasi")
                (hasil_partial, hasil_error,
                 hasil_blocked, hasil_failures,
                 hasil_rate_limited) = await _ambil_detail_publikasi(
                    page, all_rows, verbose=verbose,
                )
                partial = partial or hasil_partial
                error_message = error_message or hasil_error
                blocked = blocked or hasil_blocked
                rate_limited = rate_limited or hasil_rate_limited
                failures.extend(hasil_failures)

            # profil termuat tapi memang tidak punya publikasi
            if (not all_rows and not failures and not blocked
                    and not network_error and not parse_error):
                expected_empty = True

        finally:
            await browser.close()

    return _envelope()


# ============================================================
# OUTPUT
# ============================================================

def print_table(rows, limit=20):
    """Cetak tabel sederhana tanpa pandas (biar jalan tanpa dependensi itu).

    Dipakai run_pipeline.py untuk menampilkan cuplikan baris sebelum
    hasil scraping dimuat ke database.
    """
    if not rows:
        print("(tidak ada data)")
        return

    print()
    print("-" * 100)
    print(f"{'#':>3}  {'TAHUN':<6} {'SITASI':>7}  {'JUDUL':<58}")
    print("-" * 100)

    for i, r in enumerate(rows[:limit], start=1):
        title = (r["title"] or "").replace("\n", " ")
        if len(title) > 58:
            title = title[:55] + "..."
        year = r["year"] or "-"
        cites = r["citations"]
        print(f"{i:>3}  {year:<6} {cites:>7}  {title:<58}")

    print("-" * 100)
    if len(rows) > limit:
        print(f"... dan {len(rows) - limit} publikasi lainnya")
