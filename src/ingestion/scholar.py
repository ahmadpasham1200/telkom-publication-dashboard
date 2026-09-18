"""
Ingestion module — scraping data author & publikasi dari Google Scholar
menggunakan library `scholarly`.

Setiap run scraping harus tercatat statusnya:
SUCCESS | PARTIAL_SUCCESS | BLOCKED | TIMEOUT | NO_DATA | PARSING_ERROR

TODO:
- pindahkan logic dari notebook eksperimen ke sini
- tambahkan pencatatan run (run_id, started_at, finished_at, status,
  records_fetched, records_inserted, records_updated, records_failed,
  error_message)
- simpan hasil scraping mentah ke schema `raw` (raw.scholar_author,
  raw.scholar_publication), jangan dibersihkan di sini
"""


def scrape_author(author_query: str) -> dict:
    """Scrape data satu author dari Google Scholar berdasarkan nama/query.

    Args:
        author_query: nama dosen yang dicari di Google Scholar.

    Returns:
        dict hasil scraping mentah (belum divalidasi/dibersihkan).
    """
    raise NotImplementedError