"""Offline contract checks for Scholar detail -> RAW, without live crawling."""
import pytest
from sqlalchemy.dialects import postgresql

from src.ingestion.scrape_scholar_playwright import parse_detail_html
from src.loading import postgres


def detail(fields=(), links="", title="Article"):
    return f'<div id="gsc_oci_title">{title}</div>' + ''.join(
        f'<div class="gs_scl"><div class="gsc_oci_field">{k}</div>'
        f'<div class="gsc_oci_value">{v}</div></div>' for k, v in fields
    ) + links


@pytest.mark.parametrize('label,value,column,kind', [
    ('Journal', 'A Journal', 'detail_journal', 'journal'),
    ('Conference', '2024 COMNETSAT', 'detail_conference', 'conference'),
    ('Book', 'Collected Works', 'detail_book', 'book'),
    ('Book chapter', 'Chapter 3', 'detail_book', 'book_chapter'),
    ('Thesis', 'University', 'detail_venue', 'thesis'),
    ('Dissertation', 'University', 'detail_venue', 'thesis'),
])
def test_publication_types(label, value, column, kind):
    parsed = parse_detail_html(detail([(label, value)]))
    assert parsed[column] == value
    assert parsed['detail_publication_type'] == kind
    if kind == 'journal':
        assert parsed['detail_doi'] is None
        assert parsed['detail_description'] is None
        assert parsed['detail_citations'] is None


def test_unknown_and_missing_metadata():
    parsed = parse_detail_html(detail())
    assert parsed['detail_publication_type'] == 'unknown'
    for key in ('detail_venue', 'detail_doi', 'detail_description', 'detail_citations'):
        assert parsed[key] is None
    assert parsed['detail_extraction_status'] == 'parsed'
    assert parse_detail_html('<html>changed layout</html>')['detail_extraction_status'] == 'unrecognized_html'


def test_full_journal_and_seventeen_field_binding():
    authors = ', '.join(f'A{i} Surname' for i in range(50))
    parsed = parse_detail_html(detail([
        ('Authors', authors), ('Publication date', '2024/5'),
        ('Journal', 'J'), ('Volume', 'XII'), ('Issue', 'S1'),
        ('Pages', 'e123'), ('Publisher', 'P'), ('Description', 'Abstract'),
        ('DOI', '10.1234/explicit'),
        ('Total citations', '<a href="/scholar?cites=123">Cited by 1,234</a>'),
    ], '<a class="gsc_oci_title_link" href="https://example.org/article">Article</a>'))
    assert parsed['detail_authors'] == authors
    assert parsed['detail_doi'] == '10.1234/explicit'
    assert parsed['detail_citations'] == '1234'
    params = postgres._stmt_sisip_raw_scholar_publication(1, 0, 'profile', parsed).compile(
        dialect=postgresql.dialect()).params
    for key, value in parsed.items():
        if key != 'detail_venue_label':
            assert params[key] == value
    assert params['detail_link'] == 'https://example.org/article'


@pytest.mark.parametrize('key,href,text', [
    ('detail_cited_by', '/scholar?cites=123', 'Cited by 8'),
    ('detail_related_articles', '/scholar?q=related:abc:scholar.google.com/', 'Related articles'),
    ('detail_all_versions', '/scholar?cluster=456', 'All 3 versions'),
])
def test_relations_jsonb(key, href, text):
    parsed = parse_detail_html(detail(links=f'<a href="{href}">{text}</a>'))
    relation = parsed[key]
    assert relation['status'] == 'available'
    assert relation['items'][0]['url'] == 'https://scholar.google.com' + href
    assert relation['items'][0]['title'] is None
    assert relation['items'][0]['label'] == text
    assert relation['items'][0]['identifier']
    params = postgres._stmt_sisip_raw_scholar_publication(1, 0, 'p', parsed).compile(
        dialect=postgresql.dialect()).params
    assert params[key] == relation
    assert isinstance(postgres.RAW_SCHOLAR_PUBLICATION.c[key].type, postgresql.JSONB)


def test_missing_and_broken_relations_distinguished():
    parsed = parse_detail_html(detail(links='<a>Related articles</a>'))
    assert parsed['detail_related_articles']['status'] == 'extraction_error'
    assert parsed['detail_all_versions']['status'] == 'unavailable'


def test_malformed_optional_link_does_not_lose_metadata():
    parsed = parse_detail_html(detail([('Authors', 'A First'), ('Journal', 'J')],
        '<a href="https://[">Related articles</a>'))
    assert parsed['detail_authors'] == 'A First'
    assert parsed['detail_journal'] == 'J'
    assert parsed['detail_related_articles']['status'] == 'extraction_error'


def test_relation_label_without_links_marks_extraction_error():
    parsed = parse_detail_html(detail([('Related articles', '')]))
    assert parsed['detail_related_articles']['status'] == 'extraction_error'


def test_empty_description_is_null():
    parsed = parse_detail_html(detail(links='<div id="gsc_oci_descr"> </div>'))
    assert parsed['detail_description'] is None


def test_explicit_doi_wins_without_misleading_conflict_log(capsys):
    parsed = parse_detail_html(detail([('DOI', '10.1234/explicit')],
        '<a class="gsc_oci_title_link" href="https://doi.org/10.1234/one">Article</a>'
        '<div class="gsc_oci_title_ggi"><a href="https://doi.org/10.1234/two">PDF</a></div>'))
    assert parsed['detail_doi'] == '10.1234/explicit'
    assert 'detail_doi=NULL' not in capsys.readouterr().out


def test_linked_article_title_and_changed_tags():
    html = '<h1 id="gsc_oci_title">Article</h1><section class="gs_scl">' + (
        '<span class="gsc_oci_field">Related articles</span>'
        '<span class="gsc_oci_value"><a href="https://example.org/related">Related paper title</a></span></section>')
    item = parse_detail_html(html)['detail_related_articles']['items'][0]
    assert item['title'] == 'Related paper title'


def test_explicit_relation_context_wins_over_link_query():
    parsed = parse_detail_html(detail([('Related articles',
        '<a href="/scholar?cluster=42">A related paper</a>')]))
    assert parsed['detail_all_versions']['status'] == 'unavailable'
    item = parsed['detail_related_articles']['items'][0]
    assert item['title'] == 'A related paper'
    assert item['identifier'] == '42'


def test_missing_value_does_not_borrow_from_unrelated_block():
    html = '<div class="gsc_oci_field">Authors</div>' + detail([('Journal', 'Journal Name')])
    parsed = parse_detail_html(html)
    assert parsed['detail_authors'] is None
    assert parsed['detail_journal'] == 'Journal Name'


def test_relation_domain_and_histogram():
    parsed = parse_detail_html(detail(links=(
        '<div id="gsc_oci_graph_wrapper"><a href="/scholar?cites=9">2024</a></div>'
        '<a href="/scholar?cluster=1">All 2 versions</a>')),
        base_url='https://scholar.google.co.id/citations?view_op=view_citation')
    assert parsed['detail_cited_by']['status'] == 'unavailable'
    assert parsed['detail_all_versions']['items'][0]['url'].startswith('https://scholar.google.co.id/')


def test_explicit_type_and_partial_changed_structure():
    parsed = parse_detail_html(detail([('Publication type', 'Doctoral dissertation')]))
    assert parsed['detail_publication_type'] == 'thesis'
    parsed = parse_detail_html('<div id="gsc_oci_title">T</div><div class="gsc_oci_field">Authors</div>')
    assert parsed['detail_authors'] is None


def test_retry_preserves_existing_snapshot():
    sql = str(postgres._stmt_sisip_raw_scholar_publication(1, 0, 'p', {}).compile(
        dialect=postgresql.dialect()))
    assert 'ON CONFLICT (result_id, urutan) DO NOTHING' in sql


def test_migration_is_additive():
    from pathlib import Path
    sql = Path('sql/ddl/15_add_raw_scholar_detail_fields.sql').read_text()
    assert 'ADD COLUMN IF NOT EXISTS' in sql
    assert not any(word in sql.upper() for word in ('DROP ', 'TRUNCATE ', 'DELETE ', 'UPDATE '))


@pytest.fixture(scope='module')
def isolated_db():
    """Opt-in: creates a NEW database; never migrates an existing database."""
    import os
    import uuid
    import sqlalchemy as sa
    from pathlib import Path
    if os.environ.get('SCHOLAR_RAW_INTEGRATION') != '1':
        pytest.skip('Set SCHOLAR_RAW_INTEGRATION=1 to create an isolated test database')
    admin = postgres.build_engine()
    name = 'scholar_raw_fields_test_' + uuid.uuid4().hex[:12]
    with admin.connect().execution_options(isolation_level='AUTOCOMMIT') as conn:
        conn.exec_driver_sql(f'CREATE DATABASE "{name}"')
    engine = sa.create_engine(admin.url.set(database=name))
    admin.dispose()
    # All writes below target only the freshly created, uniquely named DB.
    with engine.connect() as conn:
        conn.exec_driver_sql('CREATE SCHEMA raw')
        conn.exec_driver_sql(Path('sql/ddl/03_create_raw_scrape_result.sql').read_text(encoding='utf-8').split('CREATE INDEX')[0])
        conn.exec_driver_sql(Path('sql/ddl/04_create_raw_structured.sql').read_text(encoding='utf-8'))
        conn.exec_driver_sql(Path('sql/ddl/10_create_raw_publication_author.sql').read_text(encoding='utf-8'))
        conn.execute(sa.text("INSERT INTO raw.scholar_scrape_result (scholar_id, scraped_at, raw_envelope) VALUES ('historical', now(), '{}')"))
        conn.execute(sa.text("INSERT INTO raw.scholar_publication (result_id,urutan,scholar_id,detail_title) VALUES (1,0,'historical','Preserved')"))
        conn.commit()
    # Use the DDL file verbatim, including its explicit transaction.
    raw = engine.raw_connection()
    try:
        raw.driver_connection.autocommit = True
        with raw.cursor() as cursor:
            sql = Path('sql/ddl/15_add_raw_scholar_detail_fields.sql').read_text(encoding='utf-8')
            cursor.execute(sql)
            cursor.execute(sql)
    finally:
        # Restore transactional mode before returning this connection to pool.
        raw.driver_connection.autocommit = False
        raw.close()
    print(f'Isolated migration database retained: {name}')
    yield engine
    engine.dispose()


def test_postgres_migration_and_raw_roundtrip(isolated_db, monkeypatch):
    import sqlalchemy as sa
    import uuid
    parsed = parse_detail_html(detail([
        ('Authors', 'A First, B Second'), ('Journal', 'J'),
        ('Publication date', '2024/05'), ('Volume', '1'), ('Issue', '2'),
        ('Pages', '3-9'), ('Publisher', 'P'), ('Description', 'Abstract'),
        ('DOI', '10.1234/test'),
        ('Total citations', '<a href="/scholar?cites=123">Cited by 8</a>'),
        ('Conference', 'COMNETSAT'), ('Book', 'Proceedings Book'),
    ], '<a href="/scholar?q=related:abc">Related articles</a>'
       '<a href="/scholar?cluster=456">All 2 versions</a>'
       '<a class="gsc_oci_title_link" href="https://example.org/article">Source</a>'))
    row = {'title': 'Article', 'detail_url': 'https://scholar.google.com/citations?citation_for_view=p:a', **parsed}
    envelope = {'run_id': str(uuid.uuid4()), 'status': 'SUCCESS',
                'data': {'scholar_id': 'p', 'name': 'Profile', 'rows': [row]}}
    with isolated_db.connect() as conn:
        columns = conn.execute(sa.text("SELECT column_name,data_type FROM information_schema.columns WHERE table_schema='raw' AND table_name='scholar_publication'")).all()
        types = dict(columns)
        for key in ('detail_journal', 'detail_conference', 'detail_book', 'detail_publication_type', 'detail_extraction_status'):
            assert types[key] == 'text'
        for key in ('detail_cited_by', 'detail_related_articles', 'detail_all_versions'):
            assert types[key] == 'jsonb'
        historical = conn.execute(sa.text('SELECT detail_title,detail_book FROM raw.scholar_publication WHERE raw_publication_id=1')).one()
        assert tuple(historical) == ('Preserved', None)
        conn.rollback()
        result_id = postgres.write_raw_scrape_result(envelope, 'p', raw_conn=conn)
        stored = conn.execute(sa.select(postgres.RAW_SCHOLAR_PUBLICATION).where(
            postgres.RAW_SCHOLAR_PUBLICATION.c.result_id == result_id)).mappings().one()
        for key, value in row.items():
            if key != 'detail_venue_label':
                assert stored[key] == value
        names = conn.execute(sa.text('SELECT author_name FROM raw.scholar_publication_author WHERE raw_publication_id=:id ORDER BY author_order'), {'id': stored['raw_publication_id']}).scalars().all()
        assert names == ['A First', 'B Second']
        conn.rollback()
        envelope['data']['rows'] = [{'title': 'Article'}]
        assert postgres.write_raw_scrape_result(envelope, 'p', raw_conn=conn) == result_id
        preserved = conn.execute(sa.select(postgres.RAW_SCHOLAR_PUBLICATION).where(
            postgres.RAW_SCHOLAR_PUBLICATION.c.result_id == result_id)).mappings().one()
        assert dict(preserved) == dict(stored)
        conn.rollback()
        # New runs append partial snapshots without mutating prior metadata.
        envelope['run_id'] = str(uuid.uuid4())
        new_id = postgres.write_raw_scrape_result(envelope, 'p', raw_conn=conn)
        assert new_id != result_id
        assert conn.execute(sa.select(postgres.RAW_SCHOLAR_PUBLICATION.c.detail_cited_by).where(
            postgres.RAW_SCHOLAR_PUBLICATION.c.result_id == new_id)).scalar_one() is None
        conn.rollback()
        # An author write failure must roll back envelope + publication too.
        envelope['run_id'] = str(uuid.uuid4())
        envelope['data']['rows'] = [row]
        def fail_author(*args, **kwargs):
            raise RuntimeError('simulated author failure')
        monkeypatch.setattr(postgres, '_stmt_sisip_raw_publication_author', fail_author)
        with pytest.raises(postgres.LoaderError, match='simulated author failure'):
            postgres.write_raw_scrape_result(envelope, 'p', raw_conn=conn)
        assert conn.execute(sa.text('SELECT count(*) FROM raw.scholar_scrape_result WHERE run_id=:run_id'),
                            {'run_id': envelope['run_id']}).scalar_one() == 0
