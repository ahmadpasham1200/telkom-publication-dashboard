"""Match locally supplied, manually verified SCImago evidence; no catalogue crawl."""
import os
import re
from .init import result, read_evidence, select_evidence, issns

DOMAINS = {'www.scimagojr.com', 'scimagojr.com'}


def parse_quartiles(record):
    """Keep each year/category observation separately, including best-quartile scope."""
    observations = []
    for item in record.get('sjr', []):
        quartile = str(item.get('quartile', '')).upper()
        year = str(item.get('year', ''))
        category = item.get('category')
        if re.fullmatch(r'Q[1-4]', quartile) and re.fullmatch(r'(19|20)\d{2}', year):
            observations.append({'quartile': quartile, 'year': int(year),
                                 'category': str(category) if category else None})
    return observations


def check(candidate, evidence_path=None):
    records = read_evidence(evidence_path or os.getenv('SCIMAGO_EVIDENCE_FILE'), 'scimagojr')
    if records is None:
        return result('scimagojr', 'access_unavailable',
                      'Supply SCIMAGO_EVIDENCE_FILE with manually verified journal evidence')
    if isinstance(records, dict):
        return records
    selected = select_evidence(candidate, records, 'scimagojr', DOMAINS)
    if isinstance(selected, dict):
        return selected
    observations = [q for r in selected for q in parse_quartiles(r)]
    first = selected[0]
    if not observations:
        return result('scimagojr', 'unverified', 'Matched evidence has no valid year/quartile observations')
    return result('scimagojr', 'indexed', 'Manually supplied SCImago evidence; no inference about Scopus',
                  evidence_url=first['evidence_url'], sjr=observations,
                  issn=next(iter(issns(first.get('issn', ''))), None),
                  eissn=next(iter(issns(first.get('eissn', ''))), None),
                  metadata={'method': 'manual_evidence',
                            'evidence_urls': sorted({r['evidence_url'] for r in selected})})
