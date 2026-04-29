"""Apache log parser.

Two-layer design:
- Worker `_parse_file_for_db` streams one file and emits per-day rollup rows
  ready to be UPSERTed into DuckDB (see `app.db`).
- `parse_logs` is the public entry point used by the Flask app and the tests:
  it delegates to `app.ingestor` to parse any new/changed files into DuckDB,
  then queries the DB to build the dashboard JSON shape.
"""
from __future__ import annotations
import gzip
import re
import os
import heapq
import logging
import time
import multiprocessing
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

# "spawn" avoids the fork-in-a-threaded-parent deadlock warning (the Flask
# app has watchdog + parse threads running when we launch the pool).
_MP_CTX = multiprocessing.get_context('spawn')


def _default_workers() -> int:
    try:
        return max(1, int(os.environ.get('PARSE_WORKERS', str(multiprocessing.cpu_count()))))
    except (TypeError, ValueError):
        return 2


def _default_nice() -> int:
    try:
        return int(os.environ.get('PARSE_NICE', '10'))
    except (TypeError, ValueError):
        return 10


def _worker_init(nice_inc: int) -> None:
    """Lower worker-process priority so the laptop stays responsive during
    background ingestion. POSIX only — a no-op on Windows (no os.nice)."""
    if nice_inc and hasattr(os, 'nice'):
        try:
            os.nice(nice_inc)
        except OSError:
            pass


from app.bot_detector import is_bot

_log = logging.getLogger('parser')

LOG_PATTERN = re.compile(
    r'(?P<ip>\S+)\s+'
    r'(?P<ident>\S+)\s+'
    r'(?P<user>\S+)\s+'
    r'\[(?P<time>[^\]]+)\]\s+'
    r'"(?P<request>[^"]*?)"\s+'
    r'(?P<status>\d{3})\s+'
    r'(?P<bytes>\S+)'
    r'(?:\s+"(?P<referer>[^"]*)")?'
    r'(?:\s+"(?P<user_agent>[^"]*)")?'
    r'(?:\s+(?P<response_time>\d+))?'
)

MONTHS = {'Jan':1,'Feb':2,'Mar':3,'Apr':4,'May':5,'Jun':6,
           'Jul':7,'Aug':8,'Sep':9,'Oct':10,'Nov':11,'Dec':12}

_VALID_METHODS = frozenset({
    'GET', 'HEAD', 'POST', 'PUT', 'DELETE', 'CONNECT', 'OPTIONS',
    'TRACE', 'PATCH', 'PROPFIND', 'PROPPATCH', 'MKCOL', 'COPY',
    'MOVE', 'LOCK', 'UNLOCK', 'PRI', 'SEARCH',
})

# Tunables — overridable via env vars
def _int_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default

_DRILL_IPS_PER_DAY   = _int_env('DRILL_IPS_PER_DAY',   500)
_DRILL_PATHS_PER_IP  = _int_env('DRILL_PATHS_PER_IP',   20)
_DRILL_PATHS_PER_DAY = _int_env('DRILL_PATHS_PER_DAY',  500)
_DRILL_IPS_PER_PATH  = _int_env('DRILL_IPS_PER_PATH',   20)
# Track 4× the final caps so ranking stays accurate even with the early-stop.
_MAX_DRILL_TRACK     = _DRILL_IPS_PER_DAY * 4
_MAX_DRILL_PATHS_PER_IP = _DRILL_PATHS_PER_IP * 4
# Per-bucket accumulation caps: bound memory growth for high-cardinality fields.
# Output needs are small (top-20 UA/referers, top-1000 paths), so tracking far
# more than those limits just burns RAM for no analytical benefit.
_MAX_UA_PER_BUCKET      = _int_env('MAX_UA_PER_BUCKET',      5_000)
_MAX_REF_PER_BUCKET     = _int_env('MAX_REF_PER_BUCKET',     5_000)
_MAX_PATHS_PER_BUCKET   = _int_env('MAX_PATHS_PER_BUCKET',  50_000)
_MAX_IPS_PER_BUCKET     = _int_env('MAX_IPS_PER_BUCKET',    50_000)


def _parse_ts(ts: str):
    try:
        day, rest = ts.split('/', 1)
        month_str, rest = rest.split('/', 1)
        year, rest = rest.split(':', 1)
        time_part, _ = rest.rsplit(' ', 1)
        h, mi, s = time_part.split(':')
        return datetime(int(year), MONTHS.get(month_str, 1), int(day),
                        int(h), int(mi), int(s))
    except Exception:
        return None


def _new_day_bucket():
    """Per-day, per-category counters accumulated during a file parse."""
    return {
        'total': 0, 'bytes_total': 0, 'errors': 0, 'rt_sum': 0, 'rt_n': 0,
        'paths': Counter(), 'ips': Counter(), 'status': Counter(),
        'methods': Counter(), 'ua': Counter(), 'referers': Counter(),
    }


def _parse_file_for_db(args):
    """Worker: stream one file and emit per-day rollup rows.

    Returns ``{file, rows, bytes_read, duration_s, tables: {name: [rows]}}``
    or ``{file, error, bytes_read}`` on failure.
    """
    file_path, from_iso, to_iso = args
    from_dt = datetime.fromisoformat(from_iso)
    to_dt = datetime.fromisoformat(to_iso)
    t0 = time.time()

    # buckets[category][day] -> counters
    buckets: dict[str, dict[str, dict]] = {
        'all': defaultdict(_new_day_bucket),
        'human': defaultdict(_new_day_bucket),
        'bot': defaultdict(_new_day_bucket),
    }
    # hourly[category][hour_iso] -> int
    hourly: dict[str, Counter] = {
        'all': Counter(), 'human': Counter(), 'bot': Counter()
    }
    # drill[day][ip][path] and drill[day][path][ip]
    ip_paths_by_day: dict[str, dict[str, Counter]] = defaultdict(lambda: defaultdict(Counter))
    path_ips_by_day: dict[str, dict[str, Counter]] = defaultdict(lambda: defaultdict(Counter))

    bytes_read = 0
    rows_total = 0
    _today = datetime.now().strftime('%Y-%m-%d')
    # UA cache: log files have heavy UA repetition (same client hammers an
    # endpoint), so memoizing skips the regex on every line after the first.
    _bot_memo: dict[str, bool] = {}

    def _open():
        if file_path.lower().endswith('.gz'):
            return gzip.open(file_path, 'rt', errors='replace')
        return open(file_path, 'r', errors='replace', buffering=8 * 1024 * 1024)

    try:
        with _open() as f:
            for line in f:
                bytes_read += len(line)
                m = LOG_PATTERN.match(line)
                if not m:
                    buckets['all'][_today]['errors'] += 1
                    continue

                dt = _parse_ts(m.group('time'))
                if dt is None or dt < from_dt or dt > to_dt:
                    continue

                ip = m.group('ip')
                status = m.group('status')
                ua = m.group('user_agent') or '-'
                ref = m.group('referer') or '-'
                b = m.group('bytes')
                rt = m.group('response_time')

                parts = m.group('request').split(None, 2)
                raw_method = parts[0].upper() if parts else ''
                method = raw_method if raw_method in _VALID_METHODS else ''
                path = (parts[1].split('?')[0].rstrip('/') or '/') if len(parts) >= 2 else ''

                day = dt.strftime('%Y-%m-%d')
                hour = dt.strftime('%Y-%m-%d %H:00:00')
                bot = _bot_memo.get(ua)
                if bot is None:
                    bot = is_bot(ua)
                    _bot_memo[ua] = bot
                cat = 'bot' if bot else 'human'
                rows_total += 1

                for key in ('all', cat):
                    bkt = buckets[key][day]
                    bkt['total'] += 1
                    i_c = bkt['ips']
                    if ip in i_c or len(i_c) < _MAX_IPS_PER_BUCKET:
                        i_c[ip] += 1
                    bkt['status'][status] += 1
                    if method:
                        bkt['methods'][method] += 1
                    if path:
                        p_c = bkt['paths']
                        if path in p_c or len(p_c) < _MAX_PATHS_PER_BUCKET:
                            p_c[path] += 1
                    if ua and ua != '-':
                        u_c = bkt['ua']
                        if ua in u_c or len(u_c) < _MAX_UA_PER_BUCKET:
                            u_c[ua] += 1
                    if ref and ref not in ('-', 'http://-'):
                        r_c = bkt['referers']
                        if ref in r_c or len(r_c) < _MAX_REF_PER_BUCKET:
                            r_c[ref] += 1
                    if b and b != '-':
                        try: bkt['bytes_total'] += int(b)
                        except ValueError: pass
                    if rt:
                        try:
                            bkt['rt_sum'] += int(rt)
                            bkt['rt_n'] += 1
                        except ValueError:
                            pass
                    hourly[key][hour] += 1

                if path:
                    day_ip_map = ip_paths_by_day[day]
                    if ip in day_ip_map or len(day_ip_map) < _MAX_DRILL_TRACK:
                        ip_paths = day_ip_map[ip]
                        if path in ip_paths or len(ip_paths) < _MAX_DRILL_PATHS_PER_IP:
                            ip_paths[path] += 1
                    day_path_map = path_ips_by_day[day]
                    if path in day_path_map or len(day_path_map) < _MAX_DRILL_TRACK:
                        path_ips = day_path_map[path]
                        if ip in path_ips or len(path_ips) < _MAX_DRILL_PATHS_PER_IP:
                            path_ips[ip] += 1
    except Exception as exc:
        return {'error': repr(exc), 'file': file_path, 'bytes_read': bytes_read}

    tables = _emit_tables(buckets, hourly, ip_paths_by_day, path_ips_by_day)

    return {
        'file': file_path,
        'rows': rows_total,
        'bytes_read': bytes_read,
        'duration_s': round(time.time() - t0, 2),
        'tables': tables,
    }


def _emit_tables(buckets, hourly, ip_paths_by_day, path_ips_by_day) -> dict[str, list]:
    """Flatten in-memory per-day counters into row tuples per rollup table."""
    daily_totals, daily_paths, daily_ips = [], [], []
    daily_status, daily_methods, daily_ua, daily_refs = [], [], [], []

    for cat, by_day in buckets.items():
        for day, bkt in by_day.items():
            daily_totals.append(
                (day, cat, bkt['total'], bkt['bytes_total'], bkt['errors'],
                 bkt['rt_sum'], bkt['rt_n'])
            )
            for p, n in bkt['paths'].items():
                daily_paths.append((day, cat, p, n))
            for ip, n in bkt['ips'].items():
                daily_ips.append((day, cat, ip, n))
            for s, n in bkt['status'].items():
                daily_status.append((day, cat, s, n))
            for mt, n in bkt['methods'].items():
                daily_methods.append((day, cat, mt, n))
            for ua, n in bkt['ua'].items():
                daily_ua.append((day, cat, ua, n))
            for r, n in bkt['referers'].items():
                daily_refs.append((day, cat, r, n))

    hourly_rows = []
    for cat, counter in hourly.items():
        for h, n in counter.items():
            hourly_rows.append((h, cat, n))

    # Drill-down: cap to top N IPs per day × top M paths
    daily_ip_paths = []
    for day, ips_d in ip_paths_by_day.items():
        top_ips = heapq.nlargest(_DRILL_IPS_PER_DAY, ips_d.items(),
                                 key=lambda kv: sum(kv[1].values()))
        for ip, paths_c in top_ips:
            for p, n in paths_c.most_common(_DRILL_PATHS_PER_IP):
                daily_ip_paths.append((day, ip, p, n))

    daily_path_ips = []
    for day, paths_d in path_ips_by_day.items():
        top_paths = heapq.nlargest(_DRILL_PATHS_PER_DAY, paths_d.items(),
                                   key=lambda kv: sum(kv[1].values()))
        for p, ips_c in top_paths:
            for ip, n in ips_c.most_common(_DRILL_IPS_PER_PATH):
                daily_path_ips.append((day, p, ip, n))

    return {
        'daily_totals': daily_totals,
        'daily_paths': daily_paths,
        'daily_ips': daily_ips,
        'daily_status': daily_status,
        'daily_methods': daily_methods,
        'daily_ua': daily_ua,
        'daily_referers': daily_refs,
        'hourly_totals': hourly_rows,
        'daily_ip_paths': daily_ip_paths,
        'daily_path_ips': daily_path_ips,
    }


# ── Public API ────────────────────────────────────────────────────────────────

def _db_path_for(log_dir: str) -> str:
    """Derive a per-log-dir DB path.  Production code passes db_path explicitly."""
    return os.path.abspath(os.path.join(log_dir, 'analytics.duckdb'))


def parse_logs(log_dir: str, days: int = 30,
               from_date: str = None, to_date: str = None,
               progress_cb=None, workers: int = None,
               db_path: str = None) -> dict:
    """Ingest new/changed files into DuckDB, then query back the dashboard shape.

    Returns a dict identical to the pre-DB shape: ``all``/``human``/``bot``
    stat blocks, ``drill_ip``/``drill_path`` maps, and metadata.
    """
    from app import db  # local import: avoids pulling duckdb into worker procs

    t_start = time.time()

    cutoff = datetime.now() - timedelta(days=days)
    cutoff_day = cutoff.strftime('%Y-%m-%d')
    from_dt = datetime.fromisoformat(from_date) if from_date else cutoff
    to_dt = datetime.fromisoformat(to_date) if to_date else datetime.now()
    if to_date:
        to_dt = to_dt.replace(hour=23, minute=59, second=59)

    db_path = db_path or _db_path_for(log_dir)
    db.reset(db_path)  # switch to (or reuse) the correct DB for this log_dir

    _log.info('Parse requested: log_dir=%s window=%dd from=%s to=%s',
              log_dir, days, from_dt.date(), to_dt.date())

    from app.ingestor import ingest_dir
    ingest_info = ingest_dir(log_dir, from_dt, to_dt,
                             workers=workers, progress_cb=progress_cb)

    from_s = from_dt.strftime('%Y-%m-%d')
    to_s = to_dt.strftime('%Y-%m-%d')

    stats = {cat: db.query_stats(cat, from_s, to_s) for cat in ('all', 'human', 'bot')}
    drill_ip = db.drill_ip_map(from_s, to_s)
    drill_path = db.drill_path_map(from_s, to_s)
    geo_info = stats['all'].get('geo_info', {})

    # Merge drill_ip / drill_path / geo from the 'all' stats into top-level.
    data = {
        'window_days': days,
        'cutoff': cutoff_day,
        'from_date': from_s,
        'to_date': to_s,
        'files_parsed': ingest_info['files_ingested'],
        'files_unchanged': ingest_info['files_unchanged'],
        'files_skipped': ingest_info['files_skipped'],
        'parse_duration_s': round(time.time() - t_start, 2),
        'workers_used': ingest_info['workers_used'],
        'worker_nice': ingest_info['worker_nice'],
        'all': stats['all'],
        'human': stats['human'],
        'bot': stats['bot'],
        'drill_ip': drill_ip,
        'drill_path': drill_path,
        'geo_info': geo_info,
        'geo_available': _geo_available(),
        'top_countries': stats['all']['top_countries'],
        'top_cities': stats['all']['top_cities'],
    }

    _log.info('Parse complete: total=%s requests (human=%s, bot=%s, errors=%s) in %.2fs',
              f"{stats['all']['total_requests']:,}",
              f"{stats['human']['total_requests']:,}",
              f"{stats['bot']['total_requests']:,}",
              f"{stats['all']['errors']:,}",
              time.time() - t_start)
    return data


def _geo_available() -> bool:
    from app import geo
    return geo.is_available()
