import os
import tempfile
import shutil
from datetime import datetime, timedelta
from pathlib import Path
import pytest
from app.parser import LOG_PATTERN, _parse_ts, parse_logs, _VALID_METHODS

_MONTHS = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec']
def _fmt(dt): return dt.strftime(f'%d/{_MONTHS[dt.month-1]}/%Y:%H:%M:%S +0000')

def _line(ip='1.2.3.4', path='/index.html', status='200', dt=None, method='GET',
          bytes_='1024', ua='Mozilla/5.0', rt=None):
    dt = dt or datetime.now()
    rt_field = f' {rt}' if rt else ''
    return (f'{ip} - - [{_fmt(dt)}] "{method} {path} HTTP/1.1" {status} {bytes_} '
            f'"-" "{ua}"{rt_field}')

NOW = datetime.now()


@pytest.fixture
def log_dir():
    d = tempfile.mkdtemp()
    yield d
    shutil.rmtree(d, ignore_errors=True)


def _write(path, lines):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        f.write('\n'.join(lines) + '\n')


# ── Pattern ───────────────────────────────────────────────────────────────────

class TestLogPattern:
    def test_ip(self):
        m = LOG_PATTERN.match(_line(ip='192.168.0.1'))
        assert m.group('ip') == '192.168.0.1'

    def test_ipv6(self):
        m = LOG_PATTERN.match(_line(ip='2001:db8::1'))
        assert m.group('ip') == '2001:db8::1'

    def test_response_time(self):
        m = LOG_PATTERN.match(_line(rt='45678'))
        assert m.group('response_time') == '45678'

    def test_malformed(self):
        assert LOG_PATTERN.match('not an apache log line') is None

    def test_dash_bytes(self):
        m = LOG_PATTERN.match(_line(bytes_='-'))
        assert m.group('bytes') == '-'


class TestParseTs:
    def test_valid(self):
        dt = _parse_ts('25/Sep/2024:16:14:07 +0100')
        assert dt and dt.year == 2024 and dt.month == 9 and dt.day == 25

    def test_invalid(self):
        assert _parse_ts('nope') is None


# ── Core parsing behaviour ────────────────────────────────────────────────────

class TestWindowFilter:
    def test_excludes_old(self, log_dir):
        _write(os.path.join(log_dir, 'a.log'),
               [_line(dt=NOW), _line(dt=NOW - timedelta(days=60))])
        r = parse_logs(log_dir, days=30)
        assert r['all']['total_requests'] == 1

    def test_window_days_param(self, log_dir):
        _write(os.path.join(log_dir, 'a.log'), [
            _line(dt=NOW - timedelta(days=5)),
            _line(dt=NOW - timedelta(days=15), path='/old'),
        ])
        assert parse_logs(log_dir, days=7)['all']['total_requests'] == 1
        assert parse_logs(log_dir, days=30)['all']['total_requests'] == 2

    def test_old_filename_still_ingested(self, log_dir):
        # Filename date is no longer a skip signal — all files are parsed;
        # only the SQL query window decides what counts.
        _write(os.path.join(log_dir, 'logfile.2020-01-01'), [_line(dt=NOW)])
        r = parse_logs(log_dir, days=30)
        assert r['all']['total_requests'] == 1
        assert r['files_skipped'] == []

    def test_explicit_range(self, log_dir):
        _write(os.path.join(log_dir, 'a.log'), [
            _line(dt=NOW),
            _line(dt=NOW - timedelta(days=3), path='/three'),
            _line(dt=NOW - timedelta(days=10), path='/ten'),
        ])
        from_d = (NOW - timedelta(days=5)).strftime('%Y-%m-%d')
        to_d = NOW.strftime('%Y-%m-%d')
        r = parse_logs(log_dir, days=30, from_date=from_d, to_date=to_d)
        assert r['all']['total_requests'] == 2


class TestBotSplit:
    def test_counts_split(self, log_dir):
        _write(os.path.join(log_dir, 'a.log'), [
            _line(ua='Googlebot/2.1', dt=NOW),
            _line(ua='Googlebot/2.1', dt=NOW),
            _line(ua='Mozilla/5.0 Firefox/121', dt=NOW),
        ])
        r = parse_logs(log_dir, days=30)
        assert r['all']['total_requests'] == 3
        assert r['bot']['total_requests'] == 2
        assert r['human']['total_requests'] == 1


class TestDrillDown:
    def test_top_ips_have_paths(self, log_dir):
        _write(os.path.join(log_dir, 'a.log'), [
            _line(ip='1.1.1.1', path='/a'),
            _line(ip='1.1.1.1', path='/a'),
            _line(ip='1.1.1.1', path='/b'),
            _line(ip='2.2.2.2', path='/c'),
        ])
        r = parse_logs(log_dir, days=30)
        assert '1.1.1.1' in r['drill_ip']
        assert r['drill_ip']['1.1.1.1']['/a'] == 2

    def test_top_paths_have_ips(self, log_dir):
        _write(os.path.join(log_dir, 'a.log'), [
            _line(ip='1.1.1.1', path='/popular'),
            _line(ip='2.2.2.2', path='/popular'),
            _line(ip='1.1.1.1', path='/rare'),
        ])
        r = parse_logs(log_dir, days=30)
        assert '/popular' in r['drill_path']
        assert set(r['drill_path']['/popular']) == {'1.1.1.1', '2.2.2.2'}


class TestParseLogs:
    def test_unique_ips(self, log_dir):
        _write(os.path.join(log_dir, 'a.log'),
               [_line(ip=f'10.0.0.{i}') for i in range(5)])
        r = parse_logs(log_dir, days=30)
        assert r['all']['unique_ips'] == 5

    def test_bytes_total(self, log_dir):
        _write(os.path.join(log_dir, 'a.log'),
               [_line(bytes_='100'), _line(bytes_='200'), _line(bytes_='-')])
        r = parse_logs(log_dir, days=30)
        assert r['all']['bytes_total'] == 300

    def test_status_codes(self, log_dir):
        _write(os.path.join(log_dir, 'a.log'),
               [_line(status='200'), _line(status='404'), _line(status='500')])
        r = parse_logs(log_dir, days=30)
        codes = dict(r['all']['status_codes'])
        assert codes == {'200': 1, '404': 1, '500': 1}

    def test_methods(self, log_dir):
        _write(os.path.join(log_dir, 'a.log'),
               [_line(method='GET'), _line(method='POST'), _line(method='GET')])
        r = parse_logs(log_dir, days=30)
        assert dict(r['all']['methods']) == {'GET': 2, 'POST': 1}

    def test_cache_hit(self, log_dir):
        _write(os.path.join(log_dir, 'a.log'), [_line()])
        r1 = parse_logs(log_dir, days=30)
        r2 = parse_logs(log_dir, days=30)
        assert r1['all']['total_requests'] == r2['all']['total_requests']

    def test_multiple_files(self, log_dir):
        _write(os.path.join(log_dir, 'a.log'), [_line(ip='1.1.1.1')])
        _write(os.path.join(log_dir, 'b.log'), [_line(ip='2.2.2.2')])
        r = parse_logs(log_dir, days=30)
        assert r['all']['total_requests'] == 2
        assert set(r['files_parsed']) == {'a.log', 'b.log'}


class TestMethodFiltering:
    """Methods extracted from malformed request lines must be silently dropped."""

    GARBAGE_METHODS = [
        '6\\xa0\\xb8\\x10\\x01',
        '[cobalt][10.1.1.1][x86_64][Linux][CentOS]',
        'LEAKIX',
        'SB90T6G',
        'arch',
        'quit\\n',
        't3s',
    ]

    def test_valid_methods_pass_through(self, log_dir):
        _write(os.path.join(log_dir, 'a.log'), [
            _line(method='GET'),
            _line(method='POST'),
            _line(method='DELETE'),
            _line(method='PATCH'),
        ])
        r = parse_logs(log_dir, days=30)
        methods = dict(r['all']['methods'])
        assert methods == {'GET': 1, 'POST': 1, 'DELETE': 1, 'PATCH': 1}

    def test_garbage_method_is_dropped(self, log_dir):
        lines = [_line(method='GET')]
        for garbage in self.GARBAGE_METHODS:
            lines.append(
                f'1.2.3.4 - - [{_fmt(NOW)}] "{garbage} /x HTTP/1.0" 400 0 "-" "-"'
            )
        _write(os.path.join(log_dir, 'a.log'), lines)
        r = parse_logs(log_dir, days=30)
        methods = dict(r['all']['methods'])
        assert set(methods) == {'GET'}, (
            f"Expected only GET; got {set(methods)}"
        )

    def test_valid_methods_set_is_complete(self):
        expected = {
            'GET', 'HEAD', 'POST', 'PUT', 'DELETE', 'CONNECT', 'OPTIONS',
            'TRACE', 'PATCH', 'PROPFIND', 'PROPPATCH', 'MKCOL', 'COPY',
            'MOVE', 'LOCK', 'UNLOCK', 'PRI', 'SEARCH',
        }
        assert _VALID_METHODS == expected

    def test_method_case_normalised(self, log_dir):
        # Lower-case method in log → normalised to upper, matched against whitelist
        line = f'1.2.3.4 - - [{_fmt(NOW)}] "get /x HTTP/1.1" 200 0 "-" "-"'
        _write(os.path.join(log_dir, 'a.log'), [line])
        r = parse_logs(log_dir, days=30)
        methods = dict(r['all']['methods'])
        assert 'GET' in methods

    def test_avg_response_time(self, log_dir):
        _write(os.path.join(log_dir, 'a.log'),
               [_line(rt='1000'), _line(rt='3000')])
        r = parse_logs(log_dir, days=30)
        assert r['all']['avg_response_time_us'] == 2000

    def test_metadata_keys(self, log_dir):
        _write(os.path.join(log_dir, 'a.log'), [_line()])
        r = parse_logs(log_dir, days=30)
        for k in ('window_days','cutoff','from_date','to_date','files_parsed',
                  'files_skipped','parse_duration_s','workers_used','geo_available',
                  'drill_ip','drill_path','top_countries','top_cities'):
            assert k in r

    def test_geo_disabled_when_no_db(self, log_dir):
        _write(os.path.join(log_dir, 'a.log'), [_line()])
        r = parse_logs(log_dir, days=30)
        assert r['geo_available'] is False
        assert r['top_countries'] == []

    def test_counts_errors(self, log_dir):
        _write(os.path.join(log_dir, 'a.log'), [_line(), 'garbage line'])
        r = parse_logs(log_dir, days=30)
        assert r['all']['errors'] == 1
        assert r['all']['total_requests'] == 1
