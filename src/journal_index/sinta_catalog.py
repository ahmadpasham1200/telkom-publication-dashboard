"""Fetch and persist the SINTA journal catalogue for one affiliation."""
import re
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

import requests
import sqlalchemy as sa
from bs4 import BeautifulSoup
from sqlalchemy.dialects.postgresql import insert

from .init import issns

AFFILIATION_ID = 1093
CATALOG_URL = f'https://sinta.kemdiktisaintek.go.id/journals/index/{AFFILIATION_ID}'
metadata = sa.MetaData(schema='raw')
catalog = sa.Table(
    'sinta_journal_catalog', metadata,
    sa.Column('sinta_journal_id', sa.BigInteger, primary_key=True),
    sa.Column('affiliation_id', sa.Integer, nullable=False), sa.Column('journal_name', sa.Text, nullable=False),
    sa.Column('cover_url', sa.Text), sa.Column('p_issn', sa.Text), sa.Column('e_issn', sa.Text),
    sa.Column('subject_area', sa.Text), sa.Column('sinta_rank', sa.Text),
    sa.Column('accreditation_status', sa.Text), sa.Column('garuda_status', sa.Text),
    sa.Column('garuda_journal_id', sa.BigInteger), sa.Column('impact', sa.Numeric(8, 2)),
    sa.Column('h5_index', sa.Integer), sa.Column('citations_5yr', sa.Integer), sa.Column('citations', sa.Integer),
    sa.Column('google_scholar_url', sa.Text), sa.Column('website_url', sa.Text), sa.Column('editor_url', sa.Text),
    sa.Column('profile_url', sa.Text, nullable=False), sa.Column('source_url', sa.Text),
    sa.Column('source_type', sa.Text),
    sa.Column('fetched_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
)
_RANK = re.compile(r'\bS[1-6]\b', re.I)
_PROFILE = re.compile(r'/journals/profile/(\d+)')
_GARUDA = re.compile(r'/journal/view/(\d+)')


def _text(node):
    return ' '.join(node.stripped_strings) if node else ''


def _value(label, text):
    match = re.search(rf'{re.escape(label)}\s*:?\s*(.*?)(?=\s+(?:P-?ISSN|E-?ISSN|Subject Area|Impact|H5-index|Citations(?:\s+5yr)?)\s*:|$)', text, re.I)
    return match.group(1).strip(' :,-') if match else None


def _metric(card, label):
    for item in card.select('.stat-profile .pr-txt'):
        if re.sub(r'\s+', ' ', _text(item)).strip(': ').casefold() == label.casefold():
            number = item.find_previous(class_='pr-num')
            if number is None:
                number = item.find_next(class_='pr-num')
            if number:
                return _text(number)
    # Common markup places each label and number in the same stat element.
    for item in card.select('.stat-profile > *'):
        if label.casefold() in _text(item).casefold():
            number = item.select_one('.pr-num') or item.find_next(class_='pr-num')
            if number:
                return _text(number)
    return None


def _integer(value):
    if value is None or not value.strip():
        return None
    digits = re.sub(r'[^\d-]', '', value)
    return int(digits) if digits else None


def _decimal(value):
    if value is None or not value.strip():
        return None
    try:
        return Decimal(value.strip().replace('.', '').replace(',', '.') if ',' in value else value.strip())
    except InvalidOperation as exc:
        raise ValueError(f'Invalid impact value: {value!r}') from exc


def parse_page(html, page_url, fetched_at=None):
    soup = BeautifulSoup(html or '', 'html.parser')
    cards = soup.select('.list-item.row.mt-3')
    if not cards:
        raise ValueError('SINTA catalogue structure changed: no journal cards found')
    fetched_at = fetched_at or datetime.now(timezone.utc)
    rows = []
    for card in cards:
        title_link = card.select_one('.affil-name a')
        profile_url = urljoin(page_url, title_link.get('href', '')) if title_link else ''
        profile_match = _PROFILE.search(urlparse(profile_url).path)
        name = _text(title_link)
        if not profile_match or not name:
            raise ValueError('SINTA catalogue card missing journal name or /journals/profile/{id} URL')
        links = card.select('.affil-abbrev a')
        def labeled(label):
            return next((urljoin(page_url, a.get('href', '')) for a in links
                         if label.casefold() in _text(a).casefold()), None)
        profile_id = int(profile_match.group(1))
        affiliation_link = card.select_one('.affil-loc a')
        affiliation_path = urlparse(urljoin(
            page_url, affiliation_link.get('href', '')) if affiliation_link else '').path.rstrip('/')
        affiliation_id = affiliation_path.rsplit('/', 1)[-1]
        if not affiliation_id.isdigit() or int(affiliation_id) != AFFILIATION_ID:
            observed = affiliation_id or 'missing'
            raise ValueError(f'Unexpected or missing SINTA affiliation ID {observed}')
        cover = card.select_one('.journal-cover')
        ids = [_text(x) for x in card.select('.profile-id')]
        profile_text = ' | '.join(ids)
        p_raw, e_raw = _value('P-ISSN', profile_text), _value('E-ISSN', profile_text)
        subject = _value('Subject Area', profile_text)
        if not any((p_raw, e_raw, subject)):
            # Positional fallback for SINTA's compact profile-id blocks.
            vals = [_text(x) for x in card.select('.profile-id')]
            if vals:
                p_raw = vals[0] if len(vals) > 0 else None
                e_raw = vals[1] if len(vals) > 1 else None
                subject = vals[2] if len(vals) > 2 else None
        p = next(iter(issns(p_raw)), None) if p_raw and p_raw.strip() != '0' else None
        e = next(iter(issns(e_raw)), None) if e_raw and e_raw.strip() != '0' else None
        accredited = card.select_one('.stat-prev .accredited')
        rank_text = _text(accredited)
        rank_match = _RANK.search(rank_text)
        garuda = card.select_one('.stat-prev .garuda-indexed')
        garuda_link = garuda.find_parent('a') if garuda else None
        garuda_url = urljoin(page_url, garuda_link.get('href', '')) if garuda_link else None
        garuda_match = _GARUDA.search(urlparse(garuda_url).path) if garuda_url else None
        status = accredited.get('title') or accredited.get('data-original-title') if accredited else None
        rows.append(dict(
            sinta_journal_id=profile_id, affiliation_id=AFFILIATION_ID, journal_name=name,
            cover_url=urljoin(page_url, cover.get('src')) if cover and cover.get('src') else None,
            p_issn=p, e_issn=e, subject_area=subject,
            sinta_rank=rank_match.group(0).upper() if rank_match else None,
            accreditation_status=status or (rank_text or None), garuda_status=_text(garuda) or None,
            garuda_journal_id=int(garuda_match.group(1)) if garuda_match else None,
            impact=_decimal(_metric(card, 'Impact')), h5_index=_integer(_metric(card, 'H5-index')),
            citations_5yr=_integer(_metric(card, 'Citations 5yr')), citations=_integer(_metric(card, 'Citations')),
            google_scholar_url=urljoin(page_url, links[0].get('href', '')) if links else None,
            website_url=labeled('Website'), editor_url=labeled('Editor URL'), profile_url=profile_url,
            source_url=page_url, fetched_at=fetched_at, created_at=fetched_at, updated_at=fetched_at,
        ))
    pages = set()
    for link in soup.select('.pagination .page-item a.page-link'):
        href = link.get('href')
        if href:
            target = urljoin(page_url, href)
            query = parse_qs(urlparse(target).query)
            if urlparse(target).path == urlparse(CATALOG_URL).path and query.get('page'):
                pages.add(CATALOG_URL if query['page'][0] == '1' else target)
    return rows, pages


_LAST_EXPLORATION = None


def fetch_catalog(session=None, delay=1.2, sleep=time.sleep, recheck=None):
    """Jalur HTTP lama dengan kontrak balikan 4-tuple yang sama; kini via HttpSource.

    recheck None berarti otomatis: pemeriksaan ulang halaman pertama aktif hanya
    untuk run nyata tanpa session injeksi (run tes memakai session tiruan).
    RunResult lengkap (status PARTIAL, peringatan) disimpan di _LAST_EXPLORATION
    agar pemanggil yang butuh detail bisa mengambilnya kembali.
    """
    global _LAST_EXPLORATION
    if recheck is None:
        recheck = session is None
    result = explore_catalog(HttpSource(session=session, delay=delay, sleep=sleep), recheck=recheck)
    _LAST_EXPLORATION = result
    if result.status == 'GAGAL':
        raise ValueError(result.error or 'Catalogue exploration failed')
    return result.rows, result.pages_processed, result.items_found, result.duplicates_dropped


def upsert_catalog(conn, rows):
    if not rows:
        return 0
    stmt = insert(catalog).values(rows)
    now = datetime.now(timezone.utc)
    excluded = stmt.excluded
    return conn.execute(stmt.on_conflict_do_update(index_elements=[catalog.c.sinta_journal_id], set_={
        **{column.name: getattr(excluded, column.name) for column in catalog.columns
           if column.name not in ('sinta_journal_id', 'created_at')},
        'updated_at': now,
    })).rowcount


# --------------------------------------------------------------------------- #
# Fungsi baru di bawah garis ini: info pagination, sumber halaman, penjelajah, #
# dan pemeriksaan kelengkapan. Fungsi parser lama di atas tidak diubah.        #
# --------------------------------------------------------------------------- #

MAX_PAGES = 50
DEFAULT_HTML_DIR = Path('data') / 'sinta' / str(AFFILIATION_ID)

_TOTAL_PATTERNS = (
    re.compile(r'showing\s+\d[\d.,]*\s*(?:(?:to|sd|sampai|–|—|-)\s*\d[\d.,]*)?\s*of\s+([\d.,]+)', re.I),
    re.compile(r'menampilkan\s+\d[\d.,]*\s*(?:(?:sd|sampai|–|—|-)\s*\d[\d.,]*)?\s*dari\s+([\d.,]+)', re.I),
)
_NEXT_LABELS = ('»', '›', 'next', 'berikutnya', 'selanjutnya')
_SAVED_FROM = re.compile(r'saved\s+from\s+url=\(\d+\)\s*(\S+)', re.I)


class SourceBlocked(Exception):
    """Server menolak akses (HTTP 403/429); run berhenti seketika tanpa retry."""

    def __init__(self, message, status=None, retry_after=None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


class RunResult:
    """Ringkasan satu run katalog: status kelengkapan, hitungan, dan peringatan."""

    def __init__(self, status, source_type, rows=None, pages_found=0, pages_processed=0,
                 items_found=0, duplicates_dropped=0, warnings=None, total_entries=None,
                 error=None, captured_at_source=None):
        self.status = status
        self.source_type = source_type
        self.rows = list(rows or [])
        self.pages_found = pages_found
        self.pages_processed = pages_processed
        self.items_found = items_found
        self.duplicates_dropped = duplicates_dropped
        self.warnings = list(warnings or [])
        self.total_entries = total_entries
        self.error = error
        self.captured_at_source = captured_at_source

    @property
    def items_unique(self):
        return len(self.rows)


def _saved_from_url(html):
    """URL asli dari komentar 'saved from url' buatan browser; None bila tidak ada."""
    match = _SAVED_FROM.search(html or '')
    if not match:
        return None
    url = match.group(1)
    if url.endswith('-->'):
        url = url[:-3].rstrip()
    return url


def read_pagination(html_or_soup, page_url):
    """Baca-saja: info pagination satu halaman katalog.

    Mengembalikan dict berisi: active (nomor halaman aktif atau None),
    active_in_markup (penanda aktif benar-benar ada di HTML), pages (daftar
    (nomor, URL absolut) dari tautan bernomor), next (tautan berikutnya bila
    punya ?page=N), total (total entri bila halaman menyediakannya, else None),
    dan affiliation_id dari path katalog.
    """
    soup = html_or_soup if hasattr(html_or_soup, 'select') else BeautifulSoup(html_or_soup or '', 'html.parser')
    pages, next_url, active, active_in_markup = [], None, None, False
    for item in soup.select('.pagination .page-item'):
        link = item.select_one('a.page-link')
        if link is None:
            continue
        label = re.sub(r'\s+', ' ', _text(link))
        href = link.get('href')
        if 'active' in (item.get('class') or []) and label.isdigit():
            active, active_in_markup = int(label), True
        if label.isdigit() and href:
            pages.append((int(label), urljoin(page_url, href)))
        elif (label.casefold() in _NEXT_LABELS or 'next' in (link.get('rel') or [])) and href:
            next_url = urljoin(page_url, href)
    query = parse_qs(urlparse(page_url).query)
    if active is None:
        if query.get('page'):
            # Nomor dari query URL hanya dipakai bila benar-benar angka;
            # fallback path katalog hanya untuk URL tanpa query page sama sekali.
            if query['page'][0].isdigit():
                active = int(query['page'][0])
        elif urlparse(page_url).path == urlparse(CATALOG_URL).path:
            active = 1
    total = None
    text = _text(soup)
    for pattern in _TOTAL_PATTERNS:
        match = pattern.search(text)
        if match:
            digits = re.sub(r'[^\d]', '', match.group(1))
            if digits:
                total = int(digits)
                break
    affiliation = None
    match = re.search(r'/journals/index/(\d+)', urlparse(page_url).path)
    if match:
        affiliation = int(match.group(1))
    return {'active': active, 'active_in_markup': active_in_markup, 'pages': pages,
            'next': next_url, 'total': total, 'affiliation_id': affiliation}


class PageFile:
    """Satu berkas HTML lokal yang sudah dipindai; nomor halaman berasal dari isi HTML."""

    def __init__(self, number, path, html, page_url, source_url, fetched_at, pagination):
        self.number = number
        self.path = path
        self.html = html
        self.page_url = page_url
        self.source_url = source_url
        self.fetched_at = fetched_at
        self.pagination = pagination


class HttpSource:
    """Sumber halaman HTTP: perilaku request sama seperti jalur lama.

    403/429 menjadi SourceBlocked yang menghentikan run seketika: tanpa retry,
    tepat satu request untuk halaman yang ditolak, dan diagnosis mencantumkan
    status, header Server, dan 300 karakter awal body tanpa cookie/header sensitif.
    Header tetap jujur (identitas aplikasi), tanpa peniruan browser.
    """

    source_type = 'http'

    def __init__(self, session=None, delay=1.2, sleep=time.sleep):
        self.session = session or requests.Session()
        self.session.headers.update({
            'User-Agent': 'TelkomPublicationDashboard/1.0 (SINTA journal catalogue sync)',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7',
            'Referer': 'https://sinta.kemdiktisaintek.go.id/',
        })
        self.delay = delay
        self.sleep = sleep
        self._requested = False

    def read(self, url):
        if self._requested and self.delay:
            self.sleep(self.delay)
        response = self.session.get(url, timeout=30)
        self._requested = True
        if response.status_code in (403, 429):
            raise self._blocked(url, response)
        response.raise_for_status()
        if not (response.text or '').strip():
            raise ValueError(f'Empty HTML from {url}')
        return response.text, url, datetime.now(timezone.utc)

    @staticmethod
    def _blocked(url, response):
        server = response.headers.get('Server') or '(header Server tidak ada)'
        body = (response.text or '')[:300]
        retry_after = response.headers.get('Retry-After')
        lines = [
            f'Akses ditolak server: HTTP {response.status_code} untuk {url}',
            f'Server: {server}',
            f'Awal body (300 karakter pertama): {body}',
        ]
        if retry_after:
            lines.append(f'Retry-After: {retry_after}')
        lines.append(
            'Run dihentikan seketika tanpa percobaan ulang dan tanpa peniruan browser. '
            'Simpan halaman katalog secara manual dari browser (Save as > Webpage, HTML only; '
            f'satu file per halaman) ke folder {DEFAULT_HTML_DIR}, lalu jalankan ulang dengan '
            '--sync-sinta-catalog --source local.')
        return SourceBlocked('\n'.join(lines), status=response.status_code, retry_after=retry_after)


class LocalHtmlSource:
    """Sumber halaman lokal: semua *.html dalam satu folder, satu sumber per run.

    Urutan halaman ditentukan dari nomor halaman aktif DI DALAM HTML (penanda
    aktif, atau URL asli dari komentar 'saved from url'/tag canonical), bukan
    dari nama file. File tanpa penanda nomor dan tanpa URL sumber ditolak;
    dua file dengan nomor halaman sama ditolak dengan menyebut kedua file.
    Menambah file halaman baru tidak membutuhkan perubahan kode.
    """

    source_type = 'local'

    def __init__(self, html_dir, captured_at=None):
        self.html_dir = Path(html_dir)
        self.captured_at = captured_at
        self.captured_at_source = 'flag --captured-at' if captured_at else 'waktu modifikasi berkas'

    def scan(self):
        """Pindai folder dan kembalikan dict nomor halaman -> PageFile."""
        paths = sorted(self.html_dir.glob('*.html'))
        if not paths:
            raise ValueError(f'Tidak ada file *.html di folder {self.html_dir}')
        files = {}
        for path in paths:
            html = path.read_text(encoding='utf-8', errors='replace')
            source_url = _saved_from_url(html)
            soup = BeautifulSoup(html, 'html.parser')
            if not source_url:
                canonical = soup.select_one('link[rel="canonical"]')
                if canonical and canonical.get('href'):
                    source_url = urljoin(CATALOG_URL, canonical['href'])
            page_url = source_url or CATALOG_URL
            info = read_pagination(soup, page_url)
            number = info['active']
            if number is None or (source_url is None and not info['active_in_markup']):
                raise ValueError(
                    f'{path.name}: nomor halaman tidak dapat ditentukan dari isi HTML '
                    '(tidak ada penanda halaman aktif dan tidak ada URL sumber '
                    '"saved from url"/canonical); penomoran dari nama file sengaja tidak dipakai')
            fetched_at = self.captured_at or datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            if number in files:
                raise ValueError(f'Dua file memiliki nomor halaman {number}: '
                                 f'{files[number].path.name} dan {path.name}')
            files[number] = PageFile(number, path, html, page_url, source_url, fetched_at, info)
        return files


class _CatalogFailure(ValueError):
    """Kegagalan penjelajahan/pemeriksaan kelengkapan yang membuat status GAGAL."""


def _dedup(rows):
    """Deduplikasi berdasarkan SINTA Profile ID; kemunculan terakhir yang dipakai."""
    unique, duplicates = {}, 0
    for row in rows:
        if row['sinta_journal_id'] in unique:
            duplicates += 1
        unique[row['sinta_journal_id']] = row
    return unique, duplicates


def _catalog_page_link(url):
    """Normalkan tautan pagination katalog: absolut, host & path katalog sama,
    memiliki ?page=N. '#' dan 'javascript:' otomatis gugur; halaman 1 dinormalkan
    ke CATALOG_URL. Mengembalikan (nomor, URL) atau None bila bukan tautan katalog."""
    if not url or url.startswith(('#', 'javascript:')):
        return None
    parsed = urlparse(url)
    base = urlparse(CATALOG_URL)
    if parsed.path != base.path or parsed.netloc != base.netloc:
        return None
    query = parse_qs(parsed.query)
    if not query.get('page') or not query['page'][0].isdigit():
        return None
    number = int(query['page'][0])
    return number, (CATALOG_URL if number == 1 else url)


def _pagination_missing(info):
    """Benar bila halaman berisi item tetapi selector pagination tidak menemukan apa pun."""
    return not info['pages'] and not info['next'] and not info['active_in_markup']


def _explore_local(source, max_pages):
    """Penjelajah sumber lokal: baca folder, deteksi halaman hilang dari tautan."""
    files = source.scan()
    warnings = []
    limit_exceeded = len(files) > max_pages
    if limit_exceeded:
        warnings.append(f'Jumlah berkas {len(files)} melebihi batas halaman maksimum {max_pages}')
    rows_all, processed, pages_found = [], [], set()
    highest_linked, total_entries = 0, None
    for number in sorted(files):
        page = files[number]
        info = page.pagination
        try:
            rows, _ = parse_page(page.html, page.page_url, fetched_at=page.fetched_at)
        except ValueError as exc:
            raise _CatalogFailure(
                f'Halaman {number} ({page.path.name}) gagal diparse: {exc}') from exc
        if _pagination_missing(info):
            raise _CatalogFailure(
                f'Halaman {number} ({page.path.name}) berisi {len(rows)} jurnal tetapi '
                'selector pagination tidak menemukan apa pun; jangan dianggap satu halaman')
        for row in rows:
            row['source_type'] = 'local'
            # URL sumber hanya dari komentar 'saved from url'/canonical; tanpa itu
            # dibiarkan kosong, tidak mengarang URL.
            row['source_url'] = page.source_url
        rows_all.extend(rows)
        processed.append(number)
        numbers = [n for n, _ in info['pages']]
        pages_found.update(numbers)
        if numbers:
            highest_linked = max(highest_linked, max(numbers))
        if info['total'] is not None and total_entries is None:
            total_entries = info['total']
    unique, duplicates = _dedup(rows_all)
    status = 'LENGKAP'
    missing = sorted(pages_found - set(files))
    if missing:
        status = 'PARTIAL'
        warnings.append('halaman ' + ', '.join(map(str, missing)) + ' belum ada di folder')
    present = sorted(files)
    if present != list(range(1, len(present) + 1)):
        status = 'PARTIAL'
        gaps = [n for n in range(1, max(present) + 1) if n not in files]
        warnings.append('nomor halaman di folder tidak berurutan 1..N; halaman tidak ada: '
                        + ', '.join(map(str, gaps)))
    if max(present) < highest_linked:
        status = 'PARTIAL'
        warnings.append(f'pagination menaut sampai halaman {highest_linked} tetapi '
                        f'halaman tertinggi di folder adalah {max(present)}')
    if total_entries is not None and len(unique) != total_entries:
        status = 'PARTIAL'
        warnings.append(f'jumlah jurnal unik {len(unique)} tidak sama dengan total entri {total_entries}')
    if limit_exceeded:
        status = 'PARTIAL'
    return RunResult(status, 'local', rows=list(unique.values()),
                     pages_found=len(pages_found | set(files)), pages_processed=len(processed),
                     items_found=len(rows_all), duplicates_dropped=duplicates,
                     warnings=warnings, total_entries=total_entries,
                     captured_at_source=source.captured_at_source)


def _explore_http(source, max_pages, recheck):
    """Penjelajah HTTP iteratif: dari tiap halaman terbaca kumpulkan tautan baru,
    pakai set kunjungan dan batas halaman maksimum; selesai dengan recheck halaman 1."""

    def run_pass():
        visited, queue = {}, [(1, CATALOG_URL)]
        rows_all, processed, pages_found = [], [], set()
        highest, total_entries, snapshot = 0, None, None
        limit_exceeded, page1_url = False, None
        warnings = []
        while queue:
            if len(processed) >= max_pages:
                limit_exceeded = True
                warnings.append(f'Batas halaman maksimum {max_pages} tercapai; '
                                'penjelajahan dihentikan lebih awal')
                break
            number, url = queue.pop(0)
            if number in visited:
                continue
            html, response_url, fetched_at = source.read(url)
            try:
                rows, _ = parse_page(html, response_url, fetched_at=fetched_at)
            except ValueError as exc:
                raise _CatalogFailure(f'Halaman {number} gagal diparse: {exc}') from exc
            info = read_pagination(html, response_url)
            if _pagination_missing(info):
                raise _CatalogFailure(
                    f'Halaman {number} berisi {len(rows)} jurnal tetapi selector pagination '
                    'tidak menemukan apa pun; jangan dianggap satu halaman')
            visited[number] = response_url
            processed.append(number)
            rows_all.extend(rows)
            numbers = [n for n, _ in info['pages']]
            pages_found.update(numbers)
            if numbers:
                highest = max(highest, max(numbers))
            if info['total'] is not None and total_entries is None:
                total_entries = info['total']
            if number == 1:
                snapshot = (info['total'], rows[0]['sinta_journal_id'] if rows else None)
                page1_url = response_url
            candidates = [normalized for _, link in info['pages']
                          for normalized in [_catalog_page_link(link)] if normalized]
            next_normalized = _catalog_page_link(info['next']) if info['next'] else None
            if next_normalized:
                candidates.append(next_normalized)
            for page_number, link in candidates:
                if page_number not in visited and page_number not in {n for n, _ in queue}:
                    queue.append((page_number, link))
        unique, duplicates = _dedup(rows_all)
        status = 'LENGKAP'
        read_numbers = sorted(visited)
        if read_numbers != list(range(1, len(read_numbers) + 1)):
            status = 'PARTIAL'
            gaps = [n for n in range(1, max(read_numbers) + 1) if n not in visited]
            warnings.append('nomor halaman tidak berurutan 1..N; halaman tidak terbaca: '
                            + ', '.join(map(str, gaps)))
        if read_numbers and max(read_numbers) < highest:
            status = 'PARTIAL'
            warnings.append(f'pagination menaut sampai halaman {highest} tetapi halaman '
                            f'tertinggi yang terbaca adalah {max(read_numbers)}')
        if total_entries is not None and len(unique) != total_entries:
            status = 'PARTIAL'
            warnings.append(f'jumlah jurnal unik {len(unique)} tidak sama dengan total entri {total_entries}')
        if limit_exceeded:
            status = 'PARTIAL'
        return {'status': status, 'rows': list(unique.values()), 'items_found': len(rows_all),
                'duplicates': duplicates, 'pages_found': len(pages_found | set(visited)),
                'pages_processed': len(processed), 'warnings': warnings,
                'total_entries': total_entries, 'snapshot': snapshot, 'page1_url': page1_url}

    def recheck_snapshot(page1_url):
        """Baca halaman pertama sekali lagi; None berarti baca/parse ulang gagal."""
        try:
            html, url, _ = source.read(page1_url)
            info = read_pagination(html, url)
            rows, _ = parse_page(html, url)
        except ValueError:
            return None
        return (info['total'], rows[0]['sinta_journal_id'] if rows else None)

    def as_result(data, extra_warnings):
        return RunResult(data['status'], 'http', rows=data['rows'], pages_found=data['pages_found'],
                         pages_processed=data['pages_processed'], items_found=data['items_found'],
                         duplicates_dropped=data['duplicates'],
                         warnings=data['warnings'] + extra_warnings,
                         total_entries=data['total_entries'])

    first = run_pass()
    if not recheck or not first['page1_url']:
        return as_result(first, [])
    extra = []
    if recheck_snapshot(first['page1_url']) != first['snapshot']:
        extra.append('Total entri atau jurnal pertama berubah selama penjelajahan; '
                     'penjelajahan diulang sekali')
        second = run_pass()
        if recheck_snapshot(second['page1_url']) != second['snapshot']:
            extra.append('Total entri atau jurnal pertama masih berubah setelah penjelajahan '
                         'diulang; status diturunkan menjadi PARTIAL')
            second['status'] = 'PARTIAL'
        return as_result(second, extra)
    return as_result(first, extra)


def explore_catalog(source, max_pages=MAX_PAGES, recheck=True):
    """Jalankan penjelajahan dengan SATU sumber halaman per run.

    SourceBlocked dibiarkan naik (run berhenti seketika); kegagalan input/parse
    menjadi RunResult berstatus GAGAL.
    """
    try:
        if isinstance(source, LocalHtmlSource):
            return _explore_local(source, max_pages)
        return _explore_http(source, max_pages, recheck)
    except ValueError as exc:
        return RunResult('GAGAL', source.source_type, warnings=[str(exc)], error=str(exc))


def run_catalog(source='http', html_dir=None, captured_at=None, max_pages=MAX_PAGES,
                session=None, delay=1.2, sleep=time.sleep, recheck=None):
    """Sinkronisasi katalog dengan SATU sumber halaman per run; kembalikan RunResult.

    source 'http' memakai jalur lama (fetch_catalog) agar kontrak CLI/tes terjaga;
    source 'local' memakai LocalHtmlSource dengan pemeriksaan kelengkapan penuh.
    """
    if source == 'local':
        return explore_catalog(LocalHtmlSource(html_dir or DEFAULT_HTML_DIR, captured_at),
                               max_pages=max_pages)
    try:
        if session is None and recheck is None:
            # Jalur default memanggil fetch_catalog() tanpa argumen agar kontrak
            # lama (termasuk penggantian fungsi pada tes CLI) tetap terjaga.
            # Hasil penjelajah penuh diambil dari _LAST_EXPLORATION bila daftar
            # rows identik; bila fetch_catalog digantikan (monkeypatch tes),
            # identitas tidak cocok dan status diturunkan dari 4-tuple saja.
            rows, pages, cards, duplicates = fetch_catalog()
            exploration = _LAST_EXPLORATION
            if exploration is not None and exploration.rows is rows:
                return exploration
            status = 'LENGKAP' if rows else 'GAGAL'
            warnings = [] if rows else ['Tidak ada jurnal terbaca dari sumber HTTP']
            return RunResult(status, 'http', rows=rows, pages_found=pages, pages_processed=pages,
                             items_found=cards, duplicates_dropped=duplicates, warnings=warnings)
        # Jalur eksplisit (session/recheck disuntik): penjelajah penuh langsung.
        return explore_catalog(HttpSource(session=session, delay=delay, sleep=sleep),
                               recheck=True if recheck is None else recheck)
    except SourceBlocked as exc:
        return RunResult('BLOCKED', 'http', warnings=[str(exc)], error=str(exc))
    except ValueError as exc:
        return RunResult('GAGAL', 'http', warnings=[str(exc)], error=str(exc))
