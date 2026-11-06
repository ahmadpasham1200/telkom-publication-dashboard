"""Interactive manual SCImago review and append-only audit history."""
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, insert

from .init import issns

metadata = sa.MetaData(schema='raw')
manual_verification = sa.Table(
    'scimago_manual_verification', metadata,
    sa.Column('manual_verification_id', sa.BigInteger, primary_key=True),
    sa.Column('journal_id', sa.BigInteger, nullable=False),
    sa.Column('confirmed_title', sa.Text, nullable=False),
    sa.Column('issn', sa.Text), sa.Column('eissn', sa.Text),
    sa.Column('decision', sa.Text, nullable=False),
    sa.Column('sjr', sa.Numeric), sa.Column('quartile', sa.Text),
    sa.Column('evidence_url', sa.Text), sa.Column('notes', sa.Text),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False,
              server_default=sa.func.now()),
)


class ManualScimagoSchemaError(ValueError):
    """The manually reviewed SCImago history migration has not been applied."""


def validate_issn(value):
    value = value.strip()
    if not value:
        return None
    if not re.fullmatch(r'\d{4}-?\d{3}[\dXx]', value):
        raise ValueError('ISSN/E-ISSN must have the format NNNN-NNNX')
    normalized = issns(value)
    if len(normalized) != 1:
        raise ValueError('ISSN/E-ISSN checksum is invalid')
    return normalized.pop()


def validate_sjr(value):
    value = value.strip()
    if not value:
        return None
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError('SJR must be a positive number or empty') from exc
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError('SJR must be a positive number or empty')
    return parsed


def validate_quartile(value):
    value = value.strip()
    if value.casefold() in {'', '-', 'kosong', 'empty', 'none'}:
        return None
    value = value.upper()
    if value and value not in {'Q1', 'Q2', 'Q3', 'Q4'}:
        raise ValueError('Quartile must be Q1, Q2, Q3, Q4, or empty/kosong')
    return value


def validate_title(value):
    value = value.strip()
    if not value:
        raise ValueError('Nama jurnal wajib diisi setelah diperiksa')
    return value


def validate_url(value):
    value = value.strip()
    if not value:
        return None
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme in {'http', 'https'} and parsed.hostname and
                 not any(char.isspace() for char in value))
        parsed.port  # reject malformed ports
    except ValueError:
        valid = False
    if not valid:
        raise ValueError('URL bukti harus berupa URL http/https yang valid atau kosong')
    return value


def list_unverified(engine):
    from . import run_journal_index as runner

    with engine.connect() as conn:
        exists = conn.execute(sa.text(
            "SELECT to_regclass('raw.scimago_manual_verification')")).scalar_one_or_none()
        if not isinstance(exists, str):
            raise ManualScimagoSchemaError(
                'Apply sql/ddl/21_create_scimago_manual_verification.sql manually first')
        rows = conn.execute(sa.select(
            runner.catalog.c.journal_id, runner.catalog.c.journal_original,
            runner.catalog.c.issn, runner.catalog.c.eissn,
            runner.table.c.reason, runner.table.c.checked_at
        ).join(runner.table, runner.table.c.journal_id == runner.catalog.c.journal_id).where(
            runner.table.c.source == 'scimagojr', runner.table.c.status == 'unverified'
        ).order_by(runner.catalog.c.journal_id)).mappings()
        return [dict(row) for row in rows]


def save_manual(engine, journal_id, entry):
    """Append an audit row and update the existing dashboard-facing status."""
    from . import run_journal_index as runner

    with engine.begin() as conn:
        exists = conn.execute(sa.text(
            "SELECT to_regclass('raw.scimago_manual_verification')")).scalar_one_or_none()
        if not isinstance(exists, str):
            raise ManualScimagoSchemaError(
                'Apply sql/ddl/21_create_scimago_manual_verification.sql manually first')
        catalog_row = conn.execute(sa.select(runner.catalog.c.journal_id).where(
            runner.catalog.c.journal_id == journal_id).with_for_update()).scalar_one_or_none()
        if catalog_row is None:
            raise ValueError(f'Unknown journal_id: {journal_id}')
        conn.execute(sa.text('SELECT pg_advisory_xact_lock(166847, :journal_id)'),
                     {'journal_id': journal_id})
        audit_id = conn.execute(insert(manual_verification).values(
            journal_id=journal_id, confirmed_title=entry['confirmed_title'],
            issn=entry['issn'], eissn=entry['eissn'], decision=entry['decision'],
            sjr=entry['sjr'], quartile=entry['quartile'],
            evidence_url=entry['evidence_url'], notes=entry['notes']
        ).returning(manual_verification.c.manual_verification_id)).scalar_one()

        now = datetime.now(timezone.utc)
        manual_status = entry['decision']
        status = {'verified': 'verified', 'unverified': 'unverified',
                  'not_journal': 'not_found'}[manual_status]
        sjr = []
        if entry['sjr'] is not None or entry['quartile'] is not None:
            sjr = [{'year': None,
                    'sjr': float(entry['sjr']) if entry['sjr'] is not None else None,
                    'quartile': entry['quartile'], 'scope': 'manual', 'category': None}]
        values = dict(
            journal_id=journal_id, source='scimagojr', status=status,
            indexed=True if status == 'verified' else False if status == 'not_found' else None,
            matched_identity={'title': entry['confirmed_title'], 'issn': entry['issn'],
                              'eissn': entry['eissn']},
            sjr=sjr, evidence_url=entry['evidence_url'], checked_at=now,
            expires_at=datetime.max.replace(tzinfo=timezone.utc),
            reason=entry['notes'] or 'Manual SCImago verification',
            metadata={'verification_type': 'manual', 'manual_status': manual_status,
                      'manual_verification_id': audit_id})
        stmt = insert(runner.table).values(**values)
        conn.execute(stmt.on_conflict_do_update(
            constraint='uq_journal_verification_source',
            set_={key: stmt.excluded[key] for key in values
                  if key not in ('journal_id', 'source')}))
    return audit_id


def collect_entry(journal, input_fn=input, output=print):
    def ask(label, hints=(), validator=None):
        while True:
            output(label)
            for hint in hints:
                output(hint)
            try:
                return validator(input_fn('Input: ')) if validator else input_fn('Input: ').strip()
            except ValueError as exc:
                output(f'Input tidak valid: {exc}')

    output('=' * 60)
    output('INPUT VERIFIKASI MANUAL SCIMAGO')
    output('=' * 60)
    title = ask('Nama jurnal yang dikonfirmasi',
                (f'Nama sebelumnya: {journal["journal_original"]}',
                 'Contoh: Journal of Information Systems'), validate_title)
    issn = ask('ISSN', ('Format: NNNN-NNNX', 'Contoh format: 1234-5679',
                        'Tekan Enter jika tidak tersedia.'), validate_issn)
    eissn = ask('E-ISSN', ('Format: NNNN-NNNX', 'Contoh format: 2049-3630',
                           'Tekan Enter jika tidak tersedia.'), validate_issn)
    decision = ask('Status verifikasi', ('Pilihan: verified / unverified / not_journal',
                                         'Contoh: verified')).lower()
    while decision not in {'verified', 'unverified', 'not_journal'}:
        output('Input tidak valid: pilih verified, unverified, atau not_journal.')
        decision = input_fn('Input: ').strip().lower()
    sjr = ask('SJR', ('Format: angka positif', 'Contoh format: 0.125',
                       'Masukkan berdasarkan bukti aktual. Tekan Enter jika tidak tersedia.'), validate_sjr)
    quartile = ask('Quartile', ('Pilihan: Q1 / Q2 / Q3 / Q4', 'Contoh: Q2',
                                'Tekan Enter jika tidak tersedia.'), validate_quartile)
    if quartile is None:
        output('Quartile: tidak tersedia (NULL)')
    evidence_url = ask('URL bukti SCImago',
                       ('Contoh: https://www.scimagojr.com/journalsearch.php?q=...',
                        'Masukkan URL halaman yang benar-benar diperiksa.'), validate_url)
    notes = ask('Catatan verifikasi',
                ('Contoh: Judul dan ISSN cocok dengan profil jurnal di SCImago.',),
                lambda value: value.strip() or None)
    entry = dict(confirmed_title=title, issn=issn, eissn=eissn,
                 decision=decision, sjr=sjr, quartile=quartile,
                 evidence_url=evidence_url, notes=notes)
    if decision == 'verified' and not evidence_url:
        output('Peringatan: status verified dipilih tanpa URL bukti.')
    output('\nRingkasan verifikasi:')
    for key, value in entry.items():
        output(f'  {key}: {value if value is not None else "(kosong)"}')
    entry['confirmed'] = input_fn('Simpan hasil ini? [y/N]: ').strip().lower() in {'y', 'yes'}
    return entry


def run(engine, input_fn=input, output=print):
    while True:
        journals = list_unverified(engine)
        output('\nJurnal SCImago berstatus unverified:')
        if not journals:
            output('(tidak ada)')
            return 0
        for journal in journals:
            output(f'{journal["journal_id"]}: {journal["journal_original"]} '
                   f'(ISSN {journal["issn"] or "-"}; E-ISSN {journal["eissn"] or "-"})')
        selected = input_fn(
            'Masukkan journal_id (Enter/selesai untuk keluar, contoh: 9): ').strip()
        if selected.casefold() in {'', 'selesai', 'exit', 'q'}:
            output('Kembali ke menu utama.')
            return 0
        if not selected.isdecimal():
            output('Input tidak valid: masukkan angka journal_id atau Enter/selesai untuk keluar.')
            continue
        journal_id = int(selected)
        journal = next((row for row in journals if row['journal_id'] == journal_id), None)
        if journal is None:
            output('journal_id tidak ada dalam daftar unverified.')
            continue
        entry = collect_entry(journal, input_fn, output)
        if not entry.pop('confirmed'):
            output('Penyimpanan dibatalkan.')
            continue
        audit_id = save_manual(engine, journal_id, entry)
        output(f'Hasil manual tersimpan (audit id: {audit_id}).')
