"""SINTA manual evidence workflow. Never automates unsupported authentication.

SINTA_USERNAME/SINTA_PASSWORD are read only as configuration presence. They are
never sent anywhere: no officially documented automated login is configured.
"""
import os
import re
from .init import result, read_evidence, select_evidence, issns

DOMAINS = {'sinta.kemdiktisaintek.go.id', 'sinta.kemdikbud.go.id'}


def parse_rank(value):
    match = re.fullmatch(r'(?:S|SINTA\s*)([1-6])', str(value or '').strip(), re.I)
    return 'S' + match[1] if match else None


def check(candidate, evidence_path=None):
    if not os.getenv('SINTA_USERNAME') or not os.getenv('SINTA_PASSWORD'):
        return result('sinta', 'access_unavailable', 'SINTA_USERNAME/SINTA_PASSWORD are not configured')
    records = read_evidence(evidence_path or os.getenv('SINTA_EVIDENCE_FILE'), 'sinta')
    if records is None:
        return result('sinta', 'access_unavailable',
                      'Automated authentication is not configured; verify via official SINTA portal and supply SINTA_EVIDENCE_FILE')
    if isinstance(records, dict):
        return records
    selected = select_evidence(candidate, records, 'sinta', DOMAINS)
    if isinstance(selected, dict):
        return selected
    ranks = {parse_rank(r.get('sinta_rank')) for r in selected}
    if None in ranks or len(ranks) != 1:
        return result('sinta', 'unverified', 'Missing or conflicting SINTA rank; split historical evidence by period')
    first = selected[0]
    return result('sinta', 'indexed', 'Manually supplied SINTA evidence; period is not inferred',
                  evidence_url=first['evidence_url'], sinta_rank=ranks.pop(),
                  sinta_period=first.get('sinta_period'),
                  issn=next(iter(issns(first.get('issn', ''))), None),
                  eissn=next(iter(issns(first.get('eissn', ''))), None),
                  metadata={'method': 'manual_evidence', 'accreditations': [
                      {'rank': parse_rank(r.get('sinta_rank')), 'period': r.get('sinta_period'),
                       'evidence_url': r['evidence_url']} for r in selected]})
