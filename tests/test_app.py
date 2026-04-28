"""End-to-end API tests against the Flask app."""
import os
import json
import tempfile
import shutil
import logging
from datetime import datetime
import pytest

from app.logging_config import setup, get_recent_logs

_MONTHS = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec']
def _fmt(dt): return dt.strftime(f'%d/{_MONTHS[dt.month-1]}/%Y:%H:%M:%S +0000')


@pytest.fixture
def app_with_logs():
    """Spin up the app with a temp log directory and pre-parsed state."""
    tmp = tempfile.mkdtemp()
    logs_dir = os.path.join(tmp, 'logs')
    os.makedirs(logs_dir)

    now = datetime.now()
    lines = [
        f'1.1.1.1 - - [{_fmt(now)}] "GET /index.html HTTP/1.1" 200 1024 "-" "Mozilla/5.0 Firefox/121"',
        f'1.1.1.1 - - [{_fmt(now)}] "GET /about HTTP/1.1" 200 512 "-" "Mozilla/5.0 Firefox/121"',
        f'2.2.2.2 - - [{_fmt(now)}] "GET /index.html HTTP/1.1" 200 1024 "-" "Googlebot/2.1"',
    ]
    with open(os.path.join(logs_dir, 'a.log'), 'w') as f:
        f.write('\n'.join(lines) + '\n')

    db_path = os.path.join(tmp, 'analytics.duckdb')

    import app.app as app_module
    app_module.LOG_DIR = logs_dir
    app_module.DB_PATH = db_path
    from app.parser import parse_logs
    parse_logs(logs_dir, days=30, db_path=db_path)
    with app_module._lock:
        app_module._state['status'] = 'done'
        app_module._state['last_updated'] = 123.0
        app_module._state['from_date'] = None
        app_module._state['to_date'] = None
    yield app_module.app.test_client()
    shutil.rmtree(tmp, ignore_errors=True)


class TestStatus:
    def test_ok(self, app_with_logs):
        client = app_with_logs
        r = client.get('/api/status')
        assert r.status_code == 200
        body = r.get_json()
        assert body['status'] == 'done'
        assert 'last_updated' in body


class TestStats:
    def test_category_all(self, app_with_logs):
        client = app_with_logs
        r = client.get('/api/stats?category=all')
        assert r.status_code == 200
        d = r.get_json()
        assert d['category'] == 'all'
        assert d['total_requests'] == 3

    def test_category_human(self, app_with_logs):
        client = app_with_logs
        r = client.get('/api/stats?category=human')
        d = r.get_json()
        assert d['total_requests'] == 2

    def test_category_bot(self, app_with_logs):
        client = app_with_logs
        r = client.get('/api/stats?category=bot')
        d = r.get_json()
        assert d['total_requests'] == 1

    def test_invalid_category(self, app_with_logs):
        client = app_with_logs
        r = client.get('/api/stats?category=invalid')
        assert r.status_code == 400


class TestDrillDown:
    def test_ip(self, app_with_logs):
        client = app_with_logs
        r = client.get('/api/drill/ip?ip=1.1.1.1')
        d = r.get_json()
        assert d['ip'] == '1.1.1.1'
        assert d['total_requests'] == 2
        assert any(p[0] == '/index.html' for p in d['paths'])

    def test_path(self, app_with_logs):
        client = app_with_logs
        r = client.get('/api/drill/path?path=/index.html')
        d = r.get_json()
        assert d['path'] == '/index.html'
        assert d['total_requests'] == 2
        assert {ip[0] for ip in d['ips']} == {'1.1.1.1', '2.2.2.2'}


class TestLogsEndpoint:
    def test_returns_structured_logs(self, app_with_logs):
        client = app_with_logs
        setup()
        logging.getLogger('test').info('hello from test')
        r = client.get('/api/logs')
        assert r.status_code == 200
        d = r.get_json()
        assert 'logs' in d
        assert any('hello from test' in entry['message'] for entry in d['logs'])

    def test_level_filter(self, app_with_logs):
        client = app_with_logs
        setup()
        logging.getLogger('test').error('boom')
        logging.getLogger('test').info('fine')
        r = client.get('/api/logs?level=ERROR')
        d = r.get_json()
        assert all(entry['level'] == 'ERROR' for entry in d['logs'])
        assert any('boom' in e['message'] for e in d['logs'])


class TestAvailableYears:
    def test_returns_years_with_data(self, app_with_logs):
        client = app_with_logs
        r = client.get('/api/years?category=all')
        assert r.status_code == 200
        d = r.get_json()
        assert 'years' in d
        assert isinstance(d['years'], list)
        assert len(d['years']) > 0
        assert datetime.now().year in d['years']

    def test_invalid_category(self, app_with_logs):
        client = app_with_logs
        r = client.get('/api/years?category=invalid')
        assert r.status_code == 400

    def test_default_category_is_all(self, app_with_logs):
        client = app_with_logs
        r = client.get('/api/years')
        assert r.status_code == 200
        d = r.get_json()
        assert 'years' in d

    def test_years_sorted_descending(self, app_with_logs):
        client = app_with_logs
        r = client.get('/api/years?category=all')
        d = r.get_json()
        years = d['years']
        assert years == sorted(years, reverse=True)


class TestRingBuffer:
    def test_captures_messages(self):
        setup()
        log = logging.getLogger('ring-test')
        log.info('ping')
        log.warning('pong')
        recent = get_recent_logs(50)
        messages = [r['message'] for r in recent]
        assert 'ping' in messages
        assert 'pong' in messages
