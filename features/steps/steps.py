import gzip
import io
import os
import json
import logging
import re
import tarfile as _tarfile
from datetime import datetime, timedelta
from behave import given, when, then
from app.parser import LOG_PATTERN, _parse_ts, parse_logs

_MONTHS = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec']

def _ts(dt=None):
    dt = dt or datetime.now()
    return dt.strftime(f'%d/{_MONTHS[dt.month-1]}/%Y:%H:%M:%S +0000')

def _line(ip='1.2.3.4', path='/', status='200', dt=None, method='GET',
          bytes_='1024', ua='Mozilla/5.0 Firefox/121'):
    return (f'{ip} - - [{_ts(dt)}] "{method} {path} HTTP/1.1" {status} {bytes_} '
            f'"-" "{ua}"')

def _write_log(path, lines):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        f.write('\n'.join(lines) + '\n')


SAMPLE_LINES = [
    _line(ip='192.168.1.1', path='/index.html', status='200', ua='Mozilla/5.0 Firefox/121'),
    _line(ip='10.0.0.2',    path='/api/data',   status='201', method='POST', ua='curl/7.88'),
    _line(ip='10.0.0.3',    path='/index.html', status='200', ua='Mozilla/5.0 Chrome/120'),
    _line(ip='10.0.0.4',    path='/about',      status='404', ua='Mozilla/5.0 Firefox/121'),
    _line(ip='10.0.0.5',    path='/index.html', status='200', ua='Mozilla/5.0 Safari/17'),
    'MALFORMED LINE WITHOUT PROPER FORMAT',
    _line(ip='10.0.0.6',    path='/index.html', status='500', bytes_='0', ua='Googlebot/2.1'),
]

# ── Given ─────────────────────────────────────────────────────────────────────

@given('PARSE_WORKERS is unset')
def step_given_parse_workers_unset(context):
    os.environ.pop('PARSE_WORKERS', None)

@given('PARSE_WORKERS is set to "{value}"')
def step_given_parse_workers_set(context, value):
    os.environ['PARSE_WORKERS'] = value

@given('PARSE_NICE is set to "{value}"')
def step_given_parse_nice_set(context, value):
    os.environ['PARSE_NICE'] = value

@given('a directory of Apache log files')
def step_given_dir(context):
    log_path = os.path.join(context.tmpdir, 'logs', 'access.log')
    _write_log(log_path, SAMPLE_LINES)
    context.log_dir = os.path.dirname(log_path)

@given('a log line in Combined Log Format')
def step_given_combined(context):
    context.line = '203.0.113.5 - bob [24/Sep/2024:08:30:00 +0000] "GET /page.html HTTP/1.1" 200 4096 "http://ref.example" "TestAgent/1.0"'

@given('a log line with a response time field')
def step_given_with_rt(context):
    context.line = '203.0.113.5 - - [24/Sep/2024:08:30:00 +0000] "GET /slow HTTP/1.1" 200 512 "-" "curl/7" 987654'

@given('a sample log file with multiple request paths')
def step_given_many_paths(context):
    log_path = os.path.join(context.tmpdir, 'logs', 'access.log')
    _write_log(log_path, SAMPLE_LINES)
    context.log_dir = os.path.dirname(log_path)

@given('a sample log file with both bot and human requests')
def step_given_bot_human(context):
    log_path = os.path.join(context.tmpdir, 'logs', 'access.log')
    lines = [
        _line(ua='Mozilla/5.0 Firefox/121'),
        _line(ua='Mozilla/5.0 Chrome/120'),
        _line(ua='Googlebot/2.1'),
        _line(ua='curl/8.0'),
    ]
    _write_log(log_path, lines)
    context.log_dir = os.path.dirname(log_path)

@given('a sample log file with a high-volume IP')
def step_given_heavy_ip(context):
    log_path = os.path.join(context.tmpdir, 'logs', 'access.log')
    heavy_ip = '8.8.8.8'
    lines = [_line(ip=heavy_ip, path=f'/p{i%3}') for i in range(30)] + [_line(ip='1.1.1.1')]
    _write_log(log_path, lines)
    context.log_dir = os.path.dirname(log_path)
    context.heavy_ip = heavy_ip

@given('a sample log file with a high-volume path')
def step_given_heavy_path(context):
    log_path = os.path.join(context.tmpdir, 'logs', 'access.log')
    heavy = '/popular'
    lines = [_line(ip=f'10.0.0.{i%5}', path=heavy) for i in range(25)] + [_line(path='/other')]
    _write_log(log_path, lines)
    context.log_dir = os.path.dirname(log_path)
    context.heavy_path = heavy

@given('a sample log file spanning several months')
def step_given_multi_month(context):
    log_path = os.path.join(context.tmpdir, 'logs', 'access.log')
    now = datetime.now()
    lines = [
        _line(dt=now, path='/recent'),
        _line(dt=now - timedelta(days=5), path='/recent2'),
        _line(dt=now - timedelta(days=60), path='/old1'),
        _line(dt=now - timedelta(days=180), path='/old2'),
    ]
    _write_log(log_path, lines)
    context.log_dir = os.path.dirname(log_path)

@given('a log file named with an old date')
def step_given_old_named(context):
    log_path = os.path.join(context.tmpdir, 'logs', 'logfile.2020-01-01')
    _write_log(log_path, [_line()])
    context.log_dir = os.path.dirname(log_path)

@given('a log file containing some malformed lines')
def step_given_malformed(context):
    log_path = os.path.join(context.tmpdir, 'logs', 'access.log')
    _write_log(log_path, SAMPLE_LINES)
    context.log_dir = os.path.dirname(log_path)

@given('a sample log file that has already been parsed')
def step_given_cached(context):
    log_path = os.path.join(context.tmpdir, 'logs', 'access.log')
    _write_log(log_path, SAMPLE_LINES)
    context.log_dir = os.path.dirname(log_path)
    context.first_result = parse_logs(context.log_dir, days=30)

@given('the Flask application is running')
def step_given_flask(context):
    import app.app as app_module
    from app import db as db_module
    log_path = os.path.join(context.tmpdir, 'logs', 'access.log')
    _write_log(log_path, SAMPLE_LINES)
    db_path = os.path.join(context.tmpdir, 'flask_test.duckdb')
    app_module.LOG_DIR = os.path.dirname(log_path)
    app_module.DB_PATH = db_path
    os.environ['DB_PATH'] = db_path
    db_module.connect(db_path)
    parse_logs(app_module.LOG_DIR, days=30, db_path=db_path)
    with app_module._lock:
        app_module._state['status'] = 'done'
        app_module._state['last_updated'] = 1.0
        app_module._state['from_date'] = None
        app_module._state['to_date'] = None
    context.client = app_module.app.test_client()

@given('the Flask application is running with an empty database')
def step_given_flask_empty_db(context):
    import app.app as app_module
    from app import db as db_module
    db_path = os.path.join(context.tmpdir, 'empty_test.duckdb')
    app_module.LOG_DIR = os.path.join(context.tmpdir, 'empty_logs')
    app_module.DB_PATH = db_path
    os.environ['DB_PATH'] = db_path
    db_module.connect(db_path)
    with app_module._lock:
        app_module._state['status'] = 'done'
        app_module._state['last_updated'] = 1.0
        app_module._state['from_date'] = None
        app_module._state['to_date'] = None
    context.client = app_module.app.test_client()


@given('the Flask application is running with traffic from a known IP')
def step_given_flask_ip(context):
    import app.app as app_module
    log_path = os.path.join(context.tmpdir, 'logs', 'access.log')
    ip = '9.9.9.9'
    lines = [_line(ip=ip, path=f'/p{i%2}') for i in range(10)]
    _write_log(log_path, lines)
    app_module.LOG_DIR = os.path.dirname(log_path)
    parse_logs(app_module.LOG_DIR, days=30)
    app_module._state['status'] = 'done'
    app_module._state['last_updated'] = 1.0
    context.client = app_module.app.test_client()
    context.heavy_ip = ip

# ── When ──────────────────────────────────────────────────────────────────────

@when('I parse the log line')
def step_when_parse_line(context):
    context.match = LOG_PATTERN.match(context.line)

@when('I parse the log directory')
def step_when_parse_dir(context):
    context.result = parse_logs(context.log_dir, days=30)

@when('I parse the log directory with a 30-day window')
def step_when_parse_30d(context):
    context.result = parse_logs(context.log_dir, days=30)

@when('I parse the log directory a second time')
def step_when_parse_again(context):
    context.second_result = parse_logs(context.log_dir, days=30)

@when('I request /api/stats for the all category')
def step_when_api_stats(context):
    context.response = context.client.get('/api/stats?category=all')

@when('I request /api/logs')
def step_when_api_logs(context):
    context.response = context.client.get('/api/logs')

@when('I request the drill-down for that IP')
def step_when_drill_ip(context):
    context.response = context.client.get(f'/api/drill/ip?ip={context.heavy_ip}')

# ── Then ──────────────────────────────────────────────────────────────────────

@then('I get the IP address')
def step_then_ip(context):
    assert context.match.group('ip') == '203.0.113.5'

@then('I get the HTTP method')
def step_then_method(context):
    assert context.match.group('request').startswith('GET')

@then('I get the request path')
def step_then_path(context):
    assert '/page.html' in context.match.group('request')

@then('I get the HTTP status code')
def step_then_status(context):
    assert context.match.group('status') == '200'

@then('I get the bytes transferred')
def step_then_bytes(context):
    assert context.match.group('bytes') == '4096'

@then('I get the response time in microseconds')
def step_then_rt(context):
    assert context.match.group('response_time') == '987654'

@then('the top paths list has at most 10 entries')
def step_then_top10(context):
    assert len(context.result['all']['top_paths']) <= 10

@then('the top paths are sorted by request count descending')
def step_then_sorted(context):
    counts = [c for _, c in context.result['all']['top_paths']]
    assert counts == sorted(counts, reverse=True)

@then('the human bucket counts only human requests')
def step_then_human(context):
    assert context.result['human']['total_requests'] == 2

@then('the bot bucket counts only bot requests')
def step_then_bot(context):
    assert context.result['bot']['total_requests'] == 2

@then('the all bucket counts everything')
def step_then_all(context):
    assert context.result['all']['total_requests'] == 4

@then('drill-down data for that IP is available')
def step_then_drill_ip(context):
    assert context.heavy_ip in context.result['drill_ip']
    assert len(context.result['drill_ip'][context.heavy_ip]) > 0

@then('the paths are ordered by request count')
def step_then_drill_ordered(context):
    counts = list(context.result['drill_ip'][context.heavy_ip].values())
    assert counts == sorted(counts, reverse=True)

@then('drill-down data for that path is available')
def step_then_drill_path(context):
    assert context.heavy_path in context.result['drill_path']
    assert len(context.result['drill_path'][context.heavy_path]) > 0

@then('only recent entries are counted')
def step_then_recent_only(context):
    paths = dict(context.result['all']['all_paths'])
    assert '/recent' in paths
    assert '/recent2' in paths
    assert '/old1' not in paths
    assert '/old2' not in paths

@then('the file is listed as ingested')
def step_then_file_ingested(context):
    assert 'logfile.2020-01-01' in context.result['files_parsed']
    assert context.result['files_skipped'] == []

@then('the error count is greater than zero')
def step_then_errors(context):
    assert context.result['all']['errors'] > 0

@then('valid lines are still counted')
def step_then_valid(context):
    assert context.result['all']['total_requests'] > 0

@then('the results are identical to the first parse')
def step_then_same(context):
    assert context.second_result['all']['total_requests'] == context.first_result['all']['total_requests']

@then('the response status is 200')
def step_then_200(context):
    assert context.response.status_code == 200

@then('the response contains total_requests')
def step_then_total(context):
    assert 'total_requests' in json.loads(context.response.data)

@then('the response contains log entries')
def step_then_logs(context):
    data = json.loads(context.response.data)
    assert 'logs' in data
    assert isinstance(data['logs'], list)

@then('the response contains path counts for that IP')
def step_then_drill_response(context):
    data = json.loads(context.response.data)
    assert data['ip'] == context.heavy_ip
    assert data['total_requests'] > 0
    assert len(data['paths']) > 0

@then('the default worker count equals the CPU count')
def step_then_default_workers(context):
    import multiprocessing
    expected = multiprocessing.cpu_count()
    assert context.result['workers_used'] == expected, \
        f"expected workers_used={expected}, got {context.result['workers_used']}"

@then('the workers used is {n:d}')
def step_then_workers_used(context, n):
    assert context.result['workers_used'] == n, \
        f"expected workers_used={n}, got {context.result['workers_used']}"

@then('worker processes ran with a nice increment of {n:d}')
def step_then_worker_nice(context, n):
    reported = context.result.get('worker_nice')
    assert reported == n, f"expected worker_nice={n}, got {reported}"


# ── Re-ingest / atomicity scenario ────────────────────────────────────────────

@given('a log file with {n:d} known requests is parsed once')
def step_given_first_ingest(context, n):
    import time as _time
    log_path = os.path.join(context.tmpdir, 'logs', 'reingest.log')
    lines = [_line(ip=f'10.1.1.{i}', path=f'/page{i}') for i in range(n)]
    _write_log(log_path, lines)
    context.log_dir = os.path.dirname(log_path)
    context.db_path = os.path.join(context.tmpdir, 'reingest.duckdb')
    context.log_filename = os.path.basename(log_path)
    # First ingest — populates the DB with n rows
    parse_logs(context.log_dir, days=30, db_path=context.db_path)
    # Record the file's mtime/size AFTER the first ingest so we can verify it
    # changes after re-ingest.
    st = os.stat(log_path)
    context.first_mtime = st.st_mtime
    context.first_size = st.st_size


@when('the file is modified to contain {n:d} requests and parsed again')
def step_when_reingest(context, n):
    import time as _time
    log_path = os.path.join(context.log_dir, context.log_filename)
    # Guarantee a new mtime by sleeping briefly and rewriting the file.
    _time.sleep(0.05)
    lines = [_line(ip=f'10.2.2.{i}', path=f'/new{i}') for i in range(n)]
    _write_log(log_path, lines)
    # Disable cooldown so re-ingest is not suppressed in this test
    os.environ['REPARSE_COOLDOWN'] = '0'
    import app.db as _db
    _db._REPARSE_COOLDOWN_S = 0
    parse_logs(context.log_dir, days=30, db_path=context.db_path)
    st = os.stat(log_path)
    context.new_mtime = st.st_mtime
    context.new_size = st.st_size


@then('the total_requests in daily_totals for that source equals {n:d}')
def step_then_no_stale_rows(context, n):
    from app import db as db_module
    db_module.connect(context.db_path)
    with db_module.cursor(context.db_path) as c:
        row = c.execute(
            "SELECT SUM(total_requests) FROM daily_totals WHERE source = ? AND category = 'all'",
            [context.log_filename]
        ).fetchone()
    actual = int(row[0]) if row and row[0] is not None else 0
    assert actual == n, (
        f"Expected total_requests={n} for source '{context.log_filename}' "
        f"after re-ingest, but got {actual}. Stale rows from the first ingest "
        f"may still be present."
    )


@then('the parsed_files ledger reflects the new mtime and size')
def step_then_ledger_updated(context):
    from app import db as db_module
    db_module.connect(context.db_path)
    with db_module.cursor(context.db_path) as c:
        row = c.execute(
            "SELECT mtime, size FROM parsed_files WHERE source = ?",
            [context.log_filename]
        ).fetchone()
    assert row is not None, (
        f"No entry in parsed_files for source '{context.log_filename}'"
    )
    ledger_mtime, ledger_size = row
    assert abs(ledger_mtime - context.new_mtime) < 0.01, (
        f"parsed_files mtime ({ledger_mtime}) does not match new file mtime "
        f"({context.new_mtime})"
    )
    assert ledger_size == context.new_size, (
        f"parsed_files size ({ledger_size}) does not match new file size "
        f"({context.new_size})"
    )


# ── Progress-log scenario ──────────────────────────────────────────────────────

class _ListHandler(logging.Handler):
    """Minimal handler that appends formatted log records to a list."""
    def __init__(self, records_list):
        super().__init__(level=logging.DEBUG)
        self._records = records_list

    def emit(self, record):
        self._records.append(self.format(record))


@given('log capture is attached to the ingestor, parser, and db loggers')
def step_given_attach_log_capture(context):
    context.captured_logs = []
    handler = _ListHandler(context.captured_logs)
    handler.setFormatter(logging.Formatter('%(name)s %(levelname)s %(message)s'))
    context._log_handler = handler

    for logger_name in ('ingestor', 'parser', 'db'):
        lgr = logging.getLogger(logger_name)
        lgr.setLevel(logging.DEBUG)
        lgr.addHandler(handler)


@then('the logs contain a message indicating parse started for the file')
def step_then_log_parse_started(context):
    msgs = context.captured_logs
    pattern = re.compile(r'(queued for parse|starting parse|parsing file)', re.IGNORECASE)
    matched = [m for m in msgs if pattern.search(m)]
    assert matched, (
        "Expected a log message matching 'queued for parse' / 'starting parse' / "
        f"'parsing file', but none found.\nCaptured logs:\n" + '\n'.join(msgs)
    )


@then('the logs contain a message about GeoIP enrichment with an IP count')
def step_then_log_geo_enrichment(context):
    msgs = context.captured_logs
    # Expect something like "Enriching 5 IPs" or "geo: 5 IPs"
    pattern = re.compile(r'(enriching|geo).{0,40}\d+\s*ip', re.IGNORECASE)
    matched = [m for m in msgs if pattern.search(m)]
    assert matched, (
        "Expected a log message about GeoIP enrichment mentioning an IP count "
        "(e.g. 'Enriching N IPs'), but none found.\nCaptured logs:\n" + '\n'.join(msgs)
    )


@then('the logs contain a message about DB write with row counts per table')
def step_then_log_db_write(context):
    msgs = context.captured_logs
    # Expect something like "wrote daily_totals: 3 rows" or "rows written per table"
    pattern = re.compile(r'(wrote|rows).{0,60}(table|\d+\s*row)', re.IGNORECASE)
    matched = [m for m in msgs if pattern.search(m)]
    assert matched, (
        "Expected a log message about DB write row counts per table "
        "(e.g. 'wrote N rows to daily_totals'), but none found.\nCaptured logs:\n"
        + '\n'.join(msgs)
    )


@then('the logs contain a completion message with per-phase timing breakdown')
def step_then_log_phase_timing(context):
    msgs = context.captured_logs
    # Expect a message containing parse=, enrich=, and write= timing labels
    matched = [m for m in msgs
               if 'parse=' in m and 'enrich=' in m and 'write=' in m]
    assert matched, (
        "Expected a completion log message containing 'parse=', 'enrich=', and "
        f"'write=' timing labels, but none found.\nCaptured logs:\n" + '\n'.join(msgs)
    )


@then('the logs contain a retention enforcement result message')
def step_then_log_retention(context):
    msgs = context.captured_logs
    # Expect something like "Retention: deleted N rows" or "retention enforced"
    pattern = re.compile(r'(retention|deleted.{0,20}row|rows.{0,20}deleted)', re.IGNORECASE)
    matched = [m for m in msgs if pattern.search(m)]
    assert matched, (
        "Expected a log message about retention enforcement (e.g. 'deleted N rows'), "
        f"but none found.\nCaptured logs:\n" + '\n'.join(msgs)
    )


# ── Valid HTTP methods scenario ────────────────────────────────────────────────

_VALID_HTTP_METHODS = {
    'GET', 'HEAD', 'POST', 'PUT', 'DELETE', 'CONNECT', 'OPTIONS',
    'TRACE', 'PATCH', 'PROPFIND', 'PROPPATCH', 'MKCOL', 'COPY',
    'MOVE', 'LOCK', 'UNLOCK', 'PRI', 'SEARCH',
}

@given('a log file containing malformed request lines')
def step_given_malformed_methods(context):
    log_path = os.path.join(context.tmpdir, 'logs', 'access.log')
    lines = [
        _line(method='GET', path='/ok'),
        _line(method='POST', path='/form'),
        # Malformed — these must not appear as methods
        '1.2.3.4 - - [01/Jan/2025:00:00:00 +0000] "6\\xa0\\xb8 /scan HTTP/1.0" 400 0 "-" "-"',
        '1.2.3.4 - - [01/Jan/2025:00:00:00 +0000] "[cobalt][10.1.1.1][x86_64] /x HTTP/1.0" 400 0 "-" "-"',
        '1.2.3.4 - - [01/Jan/2025:00:00:00 +0000] "LEAKIX / HTTP/1.1" 400 0 "-" "-"',
    ]
    _write_log(log_path, lines)
    context.log_dir = os.path.dirname(log_path)

@then('the methods data contains only valid HTTP methods')
def step_then_valid_methods_only(context):
    methods = dict(context.result['all']['methods'])
    invalid = {m for m in methods if m not in _VALID_HTTP_METHODS}
    assert not invalid, (
        f"Found invalid HTTP methods in results: {invalid}. "
        f"All methods returned: {set(methods)}"
    )


# ── Reparse cooldown scenario ──────────────────────────────────────────────────

@given('a log file that has just been ingested')
def step_given_just_ingested(context):
    import time as _time
    log_path = os.path.join(context.tmpdir, 'logs', 'active.log')
    _write_log(log_path, [_line()])
    context.log_dir = os.path.dirname(log_path)
    context.db_path = os.path.join(context.tmpdir, 'cooldown.duckdb')
    parse_logs(context.log_dir, days=30, db_path=context.db_path)
    context.log_path = log_path

@when("the file's mtime changes but the cooldown has not expired")
def step_when_mtime_changes(context):
    import time as _time
    # Append a new line to change mtime/size
    with open(context.log_path, 'a') as f:
        f.write(_line(path='/new') + '\n')
    # Parse again immediately — cooldown should suppress re-ingest
    context.second_result = parse_logs(context.log_dir, days=30, db_path=context.db_path)

@then('the file is treated as current and skipped')
def step_then_skipped_by_cooldown(context):
    fname = os.path.basename(context.log_path)
    assert fname not in context.second_result['files_parsed'], (
        f"Expected '{fname}' to be skipped by cooldown but it was re-ingested. "
        f"files_parsed={context.second_result['files_parsed']}"
    )


# ── Stats API date range scenario ─────────────────────────────────────────────

@when('I request /api/stats with an explicit date range')
def step_when_api_stats_daterange(context):
    from datetime import datetime, timedelta
    future = (datetime.now() + timedelta(days=365)).strftime('%Y-%m-%d')
    past   = (datetime.now() + timedelta(days=366)).strftime('%Y-%m-%d')
    # Request a date range in the future where no data exists
    context.response = context.client.get(
        f'/api/stats?category=all&from_date={future}&to_date={past}'
    )

@then('only data within that date range is returned')
def step_then_date_range_respected(context):
    assert context.response.status_code == 200
    data = json.loads(context.response.data)
    # No logs exist in the future window — total_requests must be zero
    assert data['total_requests'] == 0, (
        f"Expected 0 requests for a future date range, got {data['total_requests']}"
    )
    assert data['from_date'] == data['from_date']  # field is present


# ── Inverted date range scenario ───────────────────────────────────────────────

@when('I request /api/stats with from_date after to_date')
def step_when_inverted_date_range(context):
    context.response = context.client.get(
        '/api/stats?category=all&from_date=2029-01-01&to_date=2025-01-01'
    )

@then('the response status is 400')
def step_then_status_400(context):
    assert context.response.status_code == 400, (
        f"Expected 400, got {context.response.status_code}"
    )

@then('the response contains a date range error')
def step_then_date_range_error(context):
    data = json.loads(context.response.data)
    assert 'error' in data, f"Response missing 'error' key: {data}"
    assert 'date' in data['error'].lower() or 'range' in data['error'].lower(), (
        f"Error message doesn't mention date/range: {data['error']}"
    )


# ── Subnet concentration scenarios ────────────────────────────────────────────

@given('the Flask application is running with traffic from multiple subnets')
def step_given_flask_multi_subnet(context):
    import app.app as app_module
    from app import db as db_module
    log_path = os.path.join(context.tmpdir, 'logs', 'access.log')
    # Three distinct /24 subnets with different volumes
    lines = (
        [_line(ip=f'10.0.1.{i}', path='/a') for i in range(1, 21)] +   # 20 hits from 10.0.1.0/24
        [_line(ip=f'10.0.2.{i}', path='/b') for i in range(1, 11)] +   # 10 hits from 10.0.2.0/24
        [_line(ip=f'10.0.3.{i}', path='/c') for i in range(1, 6)]      #  5 hits from 10.0.3.0/24
    )
    _write_log(log_path, lines)
    db_path = os.path.join(context.tmpdir, 'subnet_test.duckdb')
    app_module.LOG_DIR = os.path.dirname(log_path)
    app_module.DB_PATH = db_path
    os.environ['DB_PATH'] = db_path
    db_module.connect(db_path)
    parse_logs(app_module.LOG_DIR, days=30, db_path=db_path)
    with app_module._lock:
        app_module._state['status'] = 'done'
        app_module._state['last_updated'] = 1.0
        app_module._state['from_date'] = None
        app_module._state['to_date'] = None
    context.client = app_module.app.test_client()


@given('the Flask application is running with a dominant subnet')
def step_given_flask_dominant_subnet(context):
    import app.app as app_module
    from app import db as db_module
    log_path = os.path.join(context.tmpdir, 'logs', 'access.log')
    # One subnet has 90% of traffic — well above any reasonable alert threshold
    lines = (
        [_line(ip=f'192.168.1.{i}', path='/x') for i in range(1, 91)] +  # 90 hits
        [_line(ip=f'10.0.0.{i}',    path='/y') for i in range(1, 11)]     # 10 hits
    )
    _write_log(log_path, lines)
    db_path = os.path.join(context.tmpdir, 'dominant_subnet.duckdb')
    app_module.LOG_DIR = os.path.dirname(log_path)
    app_module.DB_PATH = db_path
    os.environ['DB_PATH'] = db_path
    db_module.connect(db_path)
    parse_logs(app_module.LOG_DIR, days=30, db_path=db_path)
    with app_module._lock:
        app_module._state['status'] = 'done'
        app_module._state['last_updated'] = 1.0
        app_module._state['from_date'] = None
        app_module._state['to_date'] = None
    context.client = app_module.app.test_client()
    context.dominant_subnet = '192.168.1'


@when('I request /api/drill/subnets')
def step_when_api_subnets(context):
    context.response = context.client.get('/api/drill/subnets?category=all')


@when('I request /api/drill/subnets for a future date range')
def step_when_api_subnets_future(context):
    context.response = context.client.get(
        '/api/drill/subnets?category=all&from_date=2099-01-01&to_date=2099-01-31'
    )


@then('the response contains subnet entries')
def step_then_subnets_present(context):
    assert context.response.status_code == 200, (
        f"Expected 200, got {context.response.status_code}"
    )
    data = json.loads(context.response.data)
    assert 'subnets' in data, f"Response missing 'subnets' key: {data}"
    assert len(data['subnets']) > 0, "Expected at least one subnet entry"


@then('subnets are sorted by request count descending')
def step_then_subnets_sorted(context):
    data = json.loads(context.response.data)
    counts = [s['count'] for s in data['subnets']]
    assert counts == sorted(counts, reverse=True), (
        f"Subnets not sorted descending: {counts}"
    )


@then('each subnet entry has ip_prefix, count, and pct fields')
def step_then_subnet_fields(context):
    data = json.loads(context.response.data)
    for s in data['subnets']:
        assert 'ip_prefix' in s, f"Missing ip_prefix in {s}"
        assert 'count' in s, f"Missing count in {s}"
        assert 'pct' in s, f"Missing pct in {s}"


@then('the dominant subnet is flagged as an alert')
def step_then_dominant_flagged(context):
    data = json.loads(context.response.data)
    assert 'subnets' in data, f"Response missing 'subnets' key: {data}"
    dominant = next(
        (s for s in data['subnets'] if s['ip_prefix'] == context.dominant_subnet),
        None
    )
    assert dominant is not None, (
        f"Dominant subnet {context.dominant_subnet} not found in {data['subnets']}"
    )
    assert dominant.get('is_alert') is True, (
        f"Expected dominant subnet to have is_alert=True, got: {dominant}"
    )


@then('the response contains an empty subnets list')
def step_then_subnets_empty(context):
    assert context.response.status_code == 200
    data = json.loads(context.response.data)
    assert 'subnets' in data
    assert data['subnets'] == [], f"Expected empty list, got {data['subnets']}"


# ── Yearly report ─────────────────────────────────────────────────────────────

@given('the Flask application is running with multi-year log data')
def step_given_flask_multiyear(context):
    import app.app as app_module
    from app import db as db_module
    log_path = os.path.join(context.tmpdir, 'logs', 'access.log')
    lines = [
        _line(ip='1.2.3.4', path='/index.html', dt=datetime(2025, m, 15))
        for m in range(1, 13)
    ]
    _write_log(log_path, lines)
    db_path = os.path.join(context.tmpdir, 'yearly_test.duckdb')
    app_module.LOG_DIR = os.path.dirname(log_path)
    app_module.DB_PATH = db_path
    os.environ['DB_PATH'] = db_path
    db_module.connect(db_path)
    parse_logs(app_module.LOG_DIR, days=366, db_path=db_path)
    with app_module._lock:
        app_module._state['status'] = 'done'
        app_module._state['last_updated'] = 1.0
        app_module._state['from_date'] = None
        app_module._state['to_date'] = None
    context.client = app_module.app.test_client()


@when('I request /api/yearly for year "{year}" and category "{category}"')
def step_when_api_yearly(context, year, category):
    context.response = context.client.get(f'/api/yearly?year={year}&category={category}')


@when('I request /api/yearly with an invalid category')
def step_when_api_yearly_bad_category(context):
    context.response = context.client.get('/api/yearly?year=2025&category=invalid')


@when('I request /api/yearly with a non-numeric year')
def step_when_api_yearly_bad_year(context):
    context.response = context.client.get('/api/yearly?year=notayear&category=all')


@then('the response contains the year "{year}"')
def step_then_year_in_response(context, year):
    data = json.loads(context.response.data)
    assert 'year' in data, f"Response missing 'year': {data}"
    assert str(data['year']) == year, f"Expected year {year}, got {data['year']}"


@then('the response contains total_requests greater than 0')
def step_then_total_requests_positive(context):
    data = json.loads(context.response.data)
    assert 'total_requests' in data, f"Response missing 'total_requests': {data}"
    assert data['total_requests'] > 0, f"Expected total_requests > 0, got {data['total_requests']}"


@then('the response contains a monthly breakdown')
def step_then_monthly_breakdown(context):
    data = json.loads(context.response.data)
    assert 'monthly' in data, f"Response missing 'monthly': {data}"
    assert isinstance(data['monthly'], list), f"Expected 'monthly' to be a list"
    assert len(data['monthly']) > 0, f"Expected non-empty 'monthly' list"
    for entry in data['monthly']:
        assert len(entry) == 2, f"Each monthly entry must be [month, count], got {entry}"


# ── Yearly report PDF download ─────────────────────────────────────────────────

@when('I request /api/yearly/download for year "{year}" and category "{category}"')
def step_when_api_yearly_download(context, year, category):
    context.response = context.client.get(f'/api/yearly/download?year={year}&category={category}')


@when('I request /api/yearly/download with a non-numeric year')
def step_when_api_yearly_download_bad_year(context):
    context.response = context.client.get('/api/yearly/download?year=notayear&category=all')


@when('I request /api/yearly/download with an invalid category')
def step_when_api_yearly_download_bad_category(context):
    context.response = context.client.get('/api/yearly/download?year=2025&category=invalid')


@then('the response Content-Type is "{expected_content_type}"')
def step_then_response_content_type(context, expected_content_type):
    actual = context.response.content_type
    assert expected_content_type in actual, (
        f"Expected Content-Type to contain '{expected_content_type}', got '{actual}'"
    )


# ── Month selection ───────────────────────────────────────────────────────────

@given('the Flask application is running with traffic in January and February 2025')
def step_given_flask_jan_feb(context):
    import app.app as app_module
    from app import db as db_module
    log_path = os.path.join(context.tmpdir, 'logs', 'access.log')
    lines = (
        [_line(ip='1.1.1.1', path='/january-page', dt=datetime(2025, 1, 15))] * 5 +
        [_line(ip='2.2.2.2', path='/february-page', dt=datetime(2025, 2, 15))] * 3
    )
    _write_log(log_path, lines)
    db_path = os.path.join(context.tmpdir, 'month_test.duckdb')
    app_module.LOG_DIR = os.path.dirname(log_path)
    app_module.DB_PATH = db_path
    os.environ['DB_PATH'] = db_path
    db_module.connect(db_path)
    parse_logs(app_module.LOG_DIR, days=730, db_path=db_path)
    with app_module._lock:
        app_module._state['status'] = 'done'
        app_module._state['last_updated'] = 1.0
        app_module._state['from_date'] = None
        app_module._state['to_date'] = None
    context.client = app_module.app.test_client()


@when('I request /api/stats for January 2025 only')
def step_when_stats_jan_2025(context):
    context.response = context.client.get(
        '/api/stats?category=all&from_date=2025-01-01&to_date=2025-01-31'
    )


@then('the response includes January 2025 traffic')
def step_then_has_jan(context):
    assert context.response.status_code == 200
    data = json.loads(context.response.data)
    paths = [p for p, _ in data['all_paths']]
    assert '/january-page' in paths, (
        f"Expected /january-page in paths but got: {paths}"
    )


@then('February 2025 traffic is absent from the response')
def step_then_no_feb(context):
    data = json.loads(context.response.data)
    paths = [p for p, _ in data['all_paths']]
    assert '/february-page' not in paths, (
        f"Expected /february-page to be absent but found it in: {paths}"
    )


# ── Available years ───────────────────────────────────────────────────────────

@when('I request /api/years with category "{category}"')
def step_when_api_years(context, category):
    context.response = context.client.get(f'/api/years?category={category}')


@then('the response contains a list of years')
def step_then_years_list(context):
    data = json.loads(context.response.data)
    assert 'years' in data, f"Response missing 'years': {data}"
    assert isinstance(data['years'], list), f"Expected 'years' to be a list"


@then('the year "{year}" is in the available years')
def step_then_year_in_available(context, year):
    data = json.loads(context.response.data)
    assert int(year) in data['years'], (
        f"Expected {year} in available years, got: {data['years']}"
    )


@then('the available years list is empty')
def step_then_years_empty(context):
    data = json.loads(context.response.data)
    assert 'years' in data, f"Response missing 'years': {data}"
    assert data['years'] == [], f"Expected empty list, got: {data['years']}"


# ── tar.gz archive ingestion scenarios ────────────────────────────────────────

def _write_tar_gz(archive_path: str, members: dict) -> None:
    """Write a .tar.gz archive.

    Parameters
    ----------
    archive_path:
        Destination path for the ``*.tar.gz`` file.
    members:
        Mapping of {member_name: content_str} — each entry becomes one file
        inside the archive.
    """
    os.makedirs(os.path.dirname(archive_path), exist_ok=True)
    with _tarfile.open(archive_path, 'w:gz') as tar:
        for name, content in members.items():
            encoded = content.encode('utf-8')
            info = _tarfile.TarInfo(name=name)
            info.size = len(encoded)
            tar.addfile(info, io.BytesIO(encoded))


@given('a gzip-compressed log file with {n:d} requests')
def step_given_gz_file(context, n):
    log_dir = os.path.join(context.tmpdir, 'gz_test')
    os.makedirs(log_dir, exist_ok=True)
    lines = [_line(ip=f'10.0.0.{i}', path=f'/page{i}') for i in range(n)]
    content = '\n'.join(lines) + '\n'
    gz_path = os.path.join(log_dir, 'access.log.gz')
    with gzip.open(gz_path, 'wt') as f:
        f.write(content)
    context.log_dir = log_dir
    context.expected_total = n


@when('I parse the log directory with progress tracking')
def step_when_parse_with_progress(context):
    context.progress_values = []

    def _capture(pct):
        context.progress_values.append(pct)

    context.result = parse_logs(context.log_dir, days=30, progress_cb=_capture)


@then('the maximum reported progress does not exceed 100 percent')
def step_then_progress_bounded(context):
    values = context.progress_values
    if values:
        max_pct = max(values) * 100
        assert max_pct <= 100.0, (
            f"Progress exceeded 100%: max was {max_pct:.1f}% "
            f"(all values: {[round(v*100,1) for v in values]})"
        )


@given('a tar.gz archive containing a single log file with {n:d} requests')
def step_given_single_member_tar_gz(context, n):
    # Use a dedicated subdirectory to avoid mixing with the Background's access.log
    log_dir = os.path.join(context.tmpdir, 'tar_single')
    os.makedirs(log_dir, exist_ok=True)
    lines = [_line(ip=f'10.0.0.{i}', path=f'/page{i}') for i in range(n)]
    content = '\n'.join(lines) + '\n'
    archive_path = os.path.join(log_dir, 'access.tar.gz')
    _write_tar_gz(archive_path, {'access.log': content})
    context.log_dir = log_dir
    context.expected_total = n


@given('a tar.gz archive containing {num_files:d} log files with {n:d} requests each')
def step_given_multi_member_tar_gz(context, num_files, n):
    # Use a dedicated subdirectory to avoid mixing with the Background's access.log
    log_dir = os.path.join(context.tmpdir, 'tar_multi')
    os.makedirs(log_dir, exist_ok=True)
    members = {}
    for f in range(num_files):
        lines = [_line(ip=f'10.{f}.0.{i}', path=f'/file{f}/page{i}') for i in range(n)]
        members[f'access_{f}.log'] = '\n'.join(lines) + '\n'
    archive_path = os.path.join(log_dir, 'multi.tar.gz')
    _write_tar_gz(archive_path, members)
    context.log_dir = log_dir
    context.expected_total = num_files * n


@given('a plain log file with {n_plain:d} requests and a tar.gz archive with {n_gz:d} requests in the same directory')
def step_given_plain_and_tar_gz(context, n_plain, n_gz):
    # Use a dedicated subdirectory to avoid mixing with the Background's access.log
    log_dir = os.path.join(context.tmpdir, 'tar_mixed')
    os.makedirs(log_dir, exist_ok=True)
    # Plain log file
    plain_lines = [_line(ip=f'10.1.0.{i}', path=f'/plain{i}') for i in range(n_plain)]
    _write_log(os.path.join(log_dir, 'plain.log'), plain_lines)
    # tar.gz archive
    gz_lines = [_line(ip=f'10.2.0.{i}', path=f'/gz{i}') for i in range(n_gz)]
    gz_content = '\n'.join(gz_lines) + '\n'
    _write_tar_gz(os.path.join(log_dir, 'archive.tar.gz'), {'archive.log': gz_content})
    context.log_dir = log_dir
    context.expected_total = n_plain + n_gz


@then('the total request count is {n:d}')
def step_then_total_request_count(context, n):
    actual = context.result['all']['total_requests']
    assert actual == n, (
        f"Expected total_requests={n} but got {actual}. "
        f"files_parsed={context.result.get('files_parsed')}, "
        f"files_skipped={context.result.get('files_skipped')}"
    )


# ── Startup short-circuit ─────────────────────────────────────────────────────

@when('start_parsing is invoked while the ledger is up to date')
def step_when_start_parsing_ledger_current(context):
    import app.app as app_module
    from app import db as db_module

    db_path = os.path.join(context.tmpdir, 'startup_test.duckdb')
    app_module.LOG_DIR = context.log_dir
    app_module.DB_PATH = db_path
    os.environ['DB_PATH'] = db_path
    db_module.connect(db_path)

    # Re-seed the ledger against the test DB path (the @given step parsed against
    # a different db_path derived from log_dir).
    parse_logs(context.log_dir, days=30, db_path=db_path)

    with app_module._lock:
        app_module._state['status'] = 'idle'
        app_module._state['progress'] = 0.0
        app_module._state['message'] = ''
        app_module._state['last_updated'] = None

    app_module.start_parsing()
    # Capture state synchronously: if start_parsing took the slow path it would
    # have set status='parsing' and spawned a thread before returning.
    with app_module._lock:
        context.status_after_start = app_module._state['status']
        context.message_after_start = app_module._state['message']


@then('the parse status is "done" without ever entering the "parsing" state')
def step_then_parse_status_done_no_parsing(context):
    assert context.status_after_start == 'done', (
        f"expected status='done' (short-circuit), got {context.status_after_start!r}"
    )


@then('the status message indicates no parsing was needed')
def step_then_status_message_skipped(context):
    msg = (context.message_after_start or '').lower()
    assert 'up to date' in msg or 'no parse' in msg or 'skip' in msg, (
        f"expected a 'no parsing needed' message, got {context.message_after_start!r}"
    )
