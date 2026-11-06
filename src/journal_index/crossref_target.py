"""Explicit test-cluster allowlist. Never reads dotenv or prints credentials."""
import json
import os
from pathlib import Path
import re

import sqlalchemy as sa

_FIELDS = {'host', 'port', 'database', 'user', 'server_address', 'server_port', 'cluster_name'}


def load_target(path, confirmed_database):
    """All connection fields must already exist in process environment.

    An allowlist is a trusted operator file, not proof of isolation alone: the
    immutable engine URL and server cluster marker are checked before dispatch.
    """
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8-sig'))
        if not isinstance(data, dict) or data.get('version') != 1 or not isinstance(data.get('targets'), list):
            raise ValueError
        targets = data['targets']
        for t in targets:
            if not isinstance(t, dict) or set(t) != _FIELDS:
                raise ValueError
            if not re.fullmatch(r'crossref_test_[a-z0-9_]{1,40}', t['database']):
                raise ValueError
            if not re.fullmatch(r'crossref_test_[a-z0-9_]{1,40}', t['user']):
                raise ValueError
            if t['host'] not in ('127.0.0.1', '::1') or not isinstance(t['server_address'], str) or not t['server_address']:
                raise ValueError
            if type(t['port']) is not int or not 1024 <= t['port'] <= 65535 or t['port'] == 5432:
                raise ValueError
            if type(t['server_port']) is not int or not 1 <= t['server_port'] <= 65535:
                raise ValueError
            if not re.fullmatch(r'crossref-test-[0-9a-f]{32}', t['cluster_name']):
                raise ValueError
        matches = [t for t in targets if t['database'] == confirmed_database]
        if len(matches) != 1:
            raise ValueError
        t = matches[0]
        for variable, field in [('POSTGRES_HOST', 'host'), ('POSTGRES_PORT', 'port'),
                                ('POSTGRES_DB', 'database'), ('POSTGRES_USER', 'user')]:
            value = os.environ.get(variable)
            if value is None or value != str(t[field]):
                raise ValueError
    except (OSError, UnicodeError, ValueError, TypeError, KeyError):
        raise ValueError('Test database requires a valid explicit allowlist and matching process host/port/database/user; dotenv fallback is forbidden') from None
    return dict(t)


def verify_target(engine, target):
    url = engine.url
    actual = (url.host, url.port, url.database, url.username)
    expected = tuple(target[k] for k in ('host', 'port', 'database', 'user'))
    if actual != expected:
        raise ValueError('Test database engine target mismatch')
    with engine.connect() as conn:
        conn.execute(sa.text('SET TRANSACTION READ ONLY'))
        row = conn.execute(sa.text('''SELECT current_database() AS database,
            session_user AS session_user, current_user AS current_user,
            inet_server_addr()::text AS server_address, inet_server_port() AS server_port,
            current_setting('cluster_name') AS cluster_name,
            (SELECT rolsuper FROM pg_roles WHERE rolname = current_user) AS superuser''')).mappings().one()
        for field in ('database', 'server_address', 'server_port', 'cluster_name'):
            if row[field] != target[field]:
                raise ValueError('Test database server identity mismatch')
        if row['session_user'] != target['user'] or row['current_user'] != target['user'] or row['superuser'] is not False:
            raise ValueError('Test database role mismatch or elevated role')
