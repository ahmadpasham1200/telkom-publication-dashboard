# Telkom Publication Dashboard

Sistem scraping data publikasi dosen dari Google Scholar, disimpan ke PostgreSQL,
dan ditampilkan melalui dashboard Apache Superset.

## Status

Tahap awal setup — struktur repo, database, dan scraping module.

## Stack

- **Scraping**: Python + `scholarly`
- **Validation**: Pandera
- **Database**: PostgreSQL (layered: raw -> staging -> core -> mart)
- **ETL / Transformation**: Python + SQL (dbt Core di tahap berikutnya)
- **Orchestration**: Prefect OSS
- **Dashboard (owner)**: Apache Superset
- **Dashboard (internal/admin)**: Streamlit (opsional)
- **Container**: Docker Compose (OrbStack untuk development)
- **Testing**: pytest

## Struktur Folder

telkom-publication-dashboard/
├── docs/               # dokumentasi (arsitektur, requirements, data dictionary, ADR)
├── src/
│   ├── ingestion/      # scraping (scholar.py)
│   ├── validation/     # skema Pandera
│   ├── transformation/ # transformasi pandas + SQL
│   ├── loading/        # load ke Postgres
│   └── pipeline/       # flow Prefect
├── tests/
├── sql/                # ddl, staging, core, mart
└── docker/

## Setup (development)

1. Copy `.env.example` menjadi `.env`, isi kredensial database.
2. Jalankan `docker compose up -d postgres` untuk database dulu.
3. Install dependency: `pip install -r requirements.txt`
4. (Detail lanjut menyusul seiring pipeline dibangun)

## Pembagian Kerja

- **Orang A (Data Engineering)**: database, scraping, validation, ETL, pipeline
- **Orang B (Analytics/Application)**: data model review, SQL mart, Superset, dashboard, testing, documentation

Setiap bagian wajib melalui peer review dari anggota tim lain sebelum di-merge.