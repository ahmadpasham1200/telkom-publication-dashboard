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
import sys
import time
import uuid
from datetime import datetime, timezone

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
RUN_STATUSES = (
    "SUCCESS",
    "PARTIAL_SUCCESS",
    "BLOCKED",
    "TIMEOUT",
    "NO_DATA",
    "PARSING_ERROR",
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
JITTER_MIN = 1.5
JITTER_MAX = 3.5


# ============================================================
# HELPER
# ============================================================

def _utc_now_iso():
    """Waktu sekarang dalam ISO-8601 UTC, contoh: 2026-09-29T08:14:02Z."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _classify_status(*, has_data, blocked, network_error, parse_error,
                     expected_empty, partial):
    """Tentukan status run. Urutan evaluasi: first match wins.

    1. fatal, tanpa data, captcha/unusual traffic  -> BLOCKED
    2. fatal, tanpa data, nav error / timeout      -> TIMEOUT
    3. fatal, tanpa data, parse HTML melempar error -> PARSING_ERROR
    4. fatal, profil termuat tapi 0 publikasi      -> NO_DATA
    5. ada data, sebagian gagal / paging terpotong -> PARTIAL_SUCCESS
    6. ada data, semua lancar                      -> SUCCESS
    """
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
        if "h-index" not in label:
            continue
        for cell in cells[1:]:
            value = cell.get_text(strip=True)
            if value.isdigit():
                return int(value)
    return None


def parse_profile_html(html):
    """Parse HTML profil Google Scholar menjadi (nama, list of dict, h_index)."""
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

        rows.append({
            "title": title_tag.get_text(strip=True) if title_tag else None,
            "authors": authors,
            "venue": venue,
            "extra": extra,
            "citations": cites,
            "year": year_tag.get_text(strip=True) if year_tag else None,
        })

    return name, rows, h_index


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


def human(seconds):
    return f"{seconds:.2f} detik"


# ============================================================
# SCRAPER
# ============================================================

async def scrape_scholar(uid, headless=True, verbose=True, max_batches=MAX_BATCHES):
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
    error_message = None
    parsed_total = 0

    def _envelope():
        """Bangun run envelope dari state saat ini."""
        has_data = bool(all_rows)
        status = _classify_status(
            has_data=has_data,
            blocked=blocked,
            network_error=network_error,
            parse_error=parse_error,
            expected_empty=expected_empty,
            partial=partial,
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
                error_message = f"HTTP {resp.status} dari server"
                await browser.close()
                return _envelope()

            # tunggu tabel publikasi muncul
            try:
                await page.wait_for_selector("#gsc_a_b", timeout=TIMEOUT_MS)
            except Exception as exc:
                html = await page.content()
                if detect_block(html):
                    print("  DIBLOKIR Google (captcha / unusual traffic).")
                    print("  Coba: --headed, ganti SCHOLAR_ID, atau pakai proxy.")
                    blocked = True
                    error_message = "captcha / unusual traffic"
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

                before = len(all_rows)
                try:
                    await more.click()
                except Exception as exc:
                    print(f"    klik gagal: {exc}")
                    partial = True
                    error_message = error_message or f"klik gagal: {exc}"
                    break

                # jeda acak supaya traffic tidak terbaca bot
                await page.wait_for_timeout(
                    random.randint(int(JITTER_MIN * 1000), int(JITTER_MAX * 1000))
                )

                html = await page.content()
                if detect_block(html):
                    print("  DIBLOKIR Google setelah beberapa batch.")
                    blocked = True
                    partial = True
                    error_message = error_message or "captcha / unusual traffic saat paging"
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
