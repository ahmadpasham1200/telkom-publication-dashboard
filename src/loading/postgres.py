"""
Load data ke PostgreSQL menggunakan SQLAlchemy + psycopg.

TODO:
- koneksi ke database menggunakan kredensial dari .env
- fungsi load ke masing-masing layer (raw, staging, core, mart)
- gunakan role etl_writer (INSERT/UPDATE saja), bukan superuser postgres
"""