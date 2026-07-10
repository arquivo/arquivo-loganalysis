"""DuckDB-backed analytics store.

Daily rollup tables hold pre-aggregated counts so the dashboard can serve any
query with a fast SQL aggregation against small tables, regardless of how many
years of raw logs are retained. Each file is ingested exactly once (tracked in
`parsed_files`); when a file changes on disk we delete its rows by `source` and
re-insert. Retention is enforced by deleting rows older than the window.
"""
from __future__ import annotations
import logging
import os
import time
import threading
from contextlib import contextmanager

import duckdb

_log = logging.getLogger('db')

DB_PATH_DEFAULT = os.environ.get('DB_PATH', './data/analytics.duckdb')
RETENTION_DAYS = int(os.environ.get('RETENTION_DAYS', '1095'))  # 3 years

# Thread-local connections: each thread has its own DuckDB connection so
# concurrent ingest threads (tests, background parse) don't clobber each other.
_tls = threading.local()
_global_path: str = DB_PATH_DEFAULT
_path_lock = threading.Lock()


def _local_conn(path: str = None) -> duckdb.DuckDBPyConnection:
    """Return (or open) the calling thread's connection to `path`."""
    p = path or _global_path
    existing_path = getattr(_tls, 'path', None)
    existing_conn = getattr(_tls, 'conn', None)
    if existing_conn is not None and existing_path == p:
        return existing_conn
    if existing_conn is not None:
        try:
            existing_conn.close()
        except Exception:
            pass
    os.makedirs(os.path.dirname(os.path.abspath(p)) or '.', exist_ok=True)
    _log.info('Opening DuckDB at %s (thread %s)', p, threading.current_thread().name)
    conn = duckdb.connect(p)
    _init_schema(conn)
    _tls.conn = conn
    _tls.path = p
    return conn


def connect(path: str = None) -> duckdb.DuckDBPyConnection:
    """Configure the global DB path and open this thread's connection."""
    global _global_path
    with _path_lock:
        if path:
            _global_path = path
    return _local_conn(_global_path)


def close() -> None:
    """Close the calling thread's connection."""
    conn = getattr(_tls, 'conn', None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
        _tls.conn = None
        _tls.path = None


@contextmanager
def cursor(path: str = None):
    yield _local_conn(path or _global_path)


# ── Schema ────────────────────────────────────────────────────────────────────

_SCHEMA = [
    # Ledger of files we have already ingested. `source` is the key used to
    # tag every row in every rollup so we can delete-then-reinsert on change.
    """CREATE TABLE IF NOT EXISTS parsed_files (
        source TEXT PRIMARY KEY,
        path TEXT NOT NULL,
        mtime DOUBLE NOT NULL,
        size BIGINT NOT NULL,
        ingested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        rows_ingested BIGINT DEFAULT 0,
        duration_s DOUBLE DEFAULT 0
    )""",

    # Per-day overall totals, one row per (day, category, source).
    """CREATE TABLE IF NOT EXISTS daily_totals (
        day DATE NOT NULL,
        category TEXT NOT NULL,
        source TEXT NOT NULL,
        total_requests BIGINT NOT NULL,
        bytes_total BIGINT NOT NULL,
        errors BIGINT NOT NULL,
        rt_sum BIGINT NOT NULL,
        rt_n BIGINT NOT NULL,
        PRIMARY KEY (day, category, source)
    )""",

    """CREATE TABLE IF NOT EXISTS daily_paths (
        day DATE NOT NULL, category TEXT NOT NULL, source TEXT NOT NULL,
        path TEXT NOT NULL, count BIGINT NOT NULL,
        PRIMARY KEY (day, category, source, path)
    )""",

    """CREATE TABLE IF NOT EXISTS daily_ips (
        day DATE NOT NULL, category TEXT NOT NULL, source TEXT NOT NULL,
        ip TEXT NOT NULL, count BIGINT NOT NULL,
        country_code TEXT, country TEXT, city TEXT,
        PRIMARY KEY (day, category, source, ip)
    )""",

    """CREATE TABLE IF NOT EXISTS daily_status (
        day DATE NOT NULL, category TEXT NOT NULL, source TEXT NOT NULL,
        status TEXT NOT NULL, count BIGINT NOT NULL,
        PRIMARY KEY (day, category, source, status)
    )""",

    """CREATE TABLE IF NOT EXISTS daily_methods (
        day DATE NOT NULL, category TEXT NOT NULL, source TEXT NOT NULL,
        method TEXT NOT NULL, count BIGINT NOT NULL,
        PRIMARY KEY (day, category, source, method)
    )""",

    """CREATE TABLE IF NOT EXISTS daily_ua (
        day DATE NOT NULL, category TEXT NOT NULL, source TEXT NOT NULL,
        user_agent TEXT NOT NULL, count BIGINT NOT NULL,
        PRIMARY KEY (day, category, source, user_agent)
    )""",

    """CREATE TABLE IF NOT EXISTS daily_referers (
        day DATE NOT NULL, category TEXT NOT NULL, source TEXT NOT NULL,
        referer TEXT NOT NULL, count BIGINT NOT NULL,
        PRIMARY KEY (day, category, source, referer)
    )""",

    """CREATE TABLE IF NOT EXISTS hourly_totals (
        hour TIMESTAMP NOT NULL, category TEXT NOT NULL, source TEXT NOT NULL,
        count BIGINT NOT NULL,
        PRIMARY KEY (hour, category, source)
    )""",

    # Pre-aggregated drill-down: for each day, top 500 IPs × top 20 paths.
    """CREATE TABLE IF NOT EXISTS daily_ip_paths (
        day DATE NOT NULL, source TEXT NOT NULL,
        ip TEXT NOT NULL, path TEXT NOT NULL, count BIGINT NOT NULL,
        PRIMARY KEY (day, source, ip, path)
    )""",

    """CREATE TABLE IF NOT EXISTS daily_path_ips (
        day DATE NOT NULL, source TEXT NOT NULL,
        path TEXT NOT NULL, ip TEXT NOT NULL, count BIGINT NOT NULL,
        PRIMARY KEY (day, source, path, ip)
    )""",

    # Indexes on source so delete_source() avoids full-table scans.
    "CREATE INDEX IF NOT EXISTS idx_daily_totals_source    ON daily_totals    (source)",
    "CREATE INDEX IF NOT EXISTS idx_daily_paths_source     ON daily_paths     (source)",
    "CREATE INDEX IF NOT EXISTS idx_daily_ips_source       ON daily_ips       (source)",
    "CREATE INDEX IF NOT EXISTS idx_daily_status_source    ON daily_status    (source)",
    "CREATE INDEX IF NOT EXISTS idx_daily_methods_source   ON daily_methods   (source)",
    "CREATE INDEX IF NOT EXISTS idx_daily_ua_source        ON daily_ua        (source)",
    "CREATE INDEX IF NOT EXISTS idx_daily_referers_source  ON daily_referers  (source)",
    "CREATE INDEX IF NOT EXISTS idx_hourly_totals_source   ON hourly_totals   (source)",
    "CREATE INDEX IF NOT EXISTS idx_daily_ip_paths_source  ON daily_ip_paths  (source)",
    "CREATE INDEX IF NOT EXISTS idx_daily_path_ips_source  ON daily_path_ips  (source)",
]

_ROLLUP_TABLES = [
    'daily_totals', 'daily_paths', 'daily_ips', 'daily_status',
    'daily_methods', 'daily_ua', 'daily_referers', 'hourly_totals',
    'daily_ip_paths', 'daily_path_ips',
]


def _init_schema(conn: duckdb.DuckDBPyConnection) -> None:
    for stmt in _SCHEMA:
        conn.execute(stmt)


# ── Ingest helpers ────────────────────────────────────────────────────────────

_REPARSE_COOLDOWN_S = int(os.environ.get('REPARSE_COOLDOWN', '300'))  # seconds


def _ledger_means_current(row, mtime: float, size: int) -> bool:
    """Decide whether a ledger row (mtime, size, ingested_epoch) implies the
    file is current relative to (mtime, size) on disk."""
    if not row:
        return False
    stored_mtime, stored_size, ingested_epoch = row
    if abs(stored_mtime - mtime) < 0.001 and stored_size == size:
        return True
    if _REPARSE_COOLDOWN_S > 0 and ingested_epoch is not None:
        if (time.time() - ingested_epoch) < _REPARSE_COOLDOWN_S:
            return True
    return False


def is_file_current(source: str, mtime: float, size: int) -> bool:
    """True if the file should be skipped.

    Skips when the stored (mtime, size) exactly matches disk, OR when the file
    was ingested recently enough that re-parsing would just be thrashing
    (active log files receive new entries every second — we don't need to
    re-ingest them more than once per REPARSE_COOLDOWN seconds).
    """
    with cursor() as c:
        row = c.execute(
            "SELECT mtime, size, epoch(ingested_at) FROM parsed_files WHERE source = ?",
            [source],
        ).fetchone()
    return _ledger_means_current(row, mtime, size)


def fetch_ledger_map(sources: list[str]) -> dict[str, tuple]:
    """Return ``{source: (mtime, size, ingested_epoch)}`` for the given sources
    in a single query. Combine with ``_ledger_means_current`` to skip N round
    trips when scanning a directory.
    """
    if not sources:
        return {}
    placeholders = ','.join(['?'] * len(sources))
    with cursor() as c:
        rows = c.execute(
            f"SELECT source, mtime, size, epoch(ingested_at) FROM parsed_files "
            f"WHERE source IN ({placeholders})",
            sources,
        ).fetchall()
    return {r[0]: (r[1], r[2], r[3]) for r in rows}


def delete_source(source: str) -> None:
    """Remove every row tagged with this source (prior to re-ingest)."""
    with cursor() as c:
        for t in _ROLLUP_TABLES:
            c.execute(f"DELETE FROM {t} WHERE source = ?", [source])
        c.execute("DELETE FROM parsed_files WHERE source = ?", [source])



def _inject_source(table: str, source: str, row: tuple) -> tuple:
    """Place `source` in the correct column position for each table."""
    # Layout: every table has source as the 2nd or 3rd column; workers emit
    # rows WITHOUT source, we inject here.
    if table == 'daily_totals':
        # (day, category, total, bytes, errors, rt_sum, rt_n) → insert source at idx 2
        return (row[0], row[1], source, *row[2:])
    if table in ('daily_paths', 'daily_ips', 'daily_status', 'daily_methods',
                 'daily_ua', 'daily_referers'):
        # (day, category, key, count [, geo...])
        return (row[0], row[1], source, *row[2:])
    if table == 'hourly_totals':
        # (hour, category, count)
        return (row[0], row[1], source, row[2])
    if table in ('daily_ip_paths', 'daily_path_ips'):
        # (day, k1, k2, count)
        return (row[0], source, *row[1:])
    raise ValueError(f"unknown table {table}")



_INSERT_CHUNK = 1000  # rows per multi-VALUES INSERT; ~2× faster than executemany


def replace_file_rollups(source: str, path: str, mtime: float, size: int,
                         tables: dict, rows: int, duration_s: float) -> None:
    """Delete all existing rows for source and insert new ones in one transaction."""
    with cursor() as c:
        c.execute("BEGIN")
        try:
            for t in _ROLLUP_TABLES:
                c.execute(f"DELETE FROM {t} WHERE source = ?", [source])
            c.execute("DELETE FROM parsed_files WHERE source = ?", [source])
            for name, row_list in tables.items():
                if not row_list:
                    continue
                injected = [_inject_source(name, source, r) for r in row_list]
                row_ph = '(' + ','.join(['?'] * len(injected[0])) + ')'
                for i in range(0, len(injected), _INSERT_CHUNK):
                    chunk = injected[i:i + _INSERT_CHUNK]
                    sql = f"INSERT INTO {name} VALUES " + ','.join([row_ph] * len(chunk))
                    flat = [v for r in chunk for v in r]
                    c.execute(sql, flat)
            c.execute(
                """INSERT INTO parsed_files
                   (source, path, mtime, size, ingested_at, rows_ingested, duration_s)
                   VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, ?, ?)""",
                [source, path, mtime, size, rows, duration_s]
            )
            c.execute("COMMIT")
            table_summary = ', '.join(f'{n}: {len(r)}' for n, r in tables.items() if r)
            total = sum(len(r) for r in tables.values())
            _log.info('Wrote %d rows across %d tables for %s (%s)',
                      total, sum(1 for r in tables.values() if r), source, table_summary)
        except Exception:
            c.execute("ROLLBACK")
            raise


def enforce_retention(days: int = None) -> int:
    """Delete rows older than the retention window. Returns 0 — DuckDB doesn't
    expose a delete row count without materializing the rows, and the value
    was only ever used for the log line."""
    days = days or RETENTION_DAYS
    with cursor() as c:
        for t in ('daily_totals', 'daily_paths', 'daily_ips', 'daily_status',
                  'daily_methods', 'daily_ua', 'daily_referers',
                  'daily_ip_paths', 'daily_path_ips'):
            c.execute(
                f"DELETE FROM {t} WHERE day < current_date - INTERVAL '{days} days'"
            )
        c.execute(
            f"DELETE FROM hourly_totals WHERE hour < now() - INTERVAL '{days} days'"
        )
    _log.info('Retention: rows older than %d days deleted', days)
    return 0


# ── Query helpers ─────────────────────────────────────────────────────────────

def query_stats(category: str, from_date: str, to_date: str) -> dict:
    """Build the stats dict consumed by the dashboard, entirely from DuckDB."""
    with cursor() as c:
        totals = c.execute(
            """SELECT COALESCE(SUM(total_requests),0),
                      COALESCE(SUM(bytes_total),0),
                      COALESCE(SUM(errors),0),
                      COALESCE(SUM(rt_sum),0),
                      COALESCE(SUM(rt_n),0)
               FROM daily_totals
               WHERE category = ? AND day BETWEEN ? AND ?""",
            [category, from_date, to_date]
        ).fetchone()

        unique_ips = c.execute(
            """SELECT COUNT(DISTINCT ip) FROM daily_ips
               WHERE category = ? AND day BETWEEN ? AND ?""",
            [category, from_date, to_date]
        ).fetchone()[0]

        # Single LIMIT 1000 scan; top_paths is just the first 10.
        all_paths = c.execute(
            """SELECT path, SUM(count) AS n FROM daily_paths
               WHERE category = ? AND day BETWEEN ? AND ?
               GROUP BY path ORDER BY n DESC LIMIT 1000""",
            [category, from_date, to_date]
        ).fetchall()
        top_paths = all_paths[:10]

        top_ips = c.execute(
            """SELECT ip, SUM(count) AS n,
                      ANY_VALUE(country_code), ANY_VALUE(country), ANY_VALUE(city)
               FROM daily_ips
               WHERE category = ? AND day BETWEEN ? AND ?
               GROUP BY ip ORDER BY n DESC LIMIT 50""",
            [category, from_date, to_date]
        ).fetchall()

        status_codes = c.execute(
            """SELECT status, SUM(count) FROM daily_status
               WHERE category = ? AND day BETWEEN ? AND ?
               GROUP BY status ORDER BY status""",
            [category, from_date, to_date]
        ).fetchall()

        methods = c.execute(
            """SELECT method, SUM(count) FROM daily_methods
               WHERE category = ? AND day BETWEEN ? AND ?
               GROUP BY method ORDER BY method""",
            [category, from_date, to_date]
        ).fetchall()

        top_ua = c.execute(
            """SELECT user_agent, SUM(count) AS n FROM daily_ua
               WHERE category = ? AND day BETWEEN ? AND ?
               GROUP BY user_agent ORDER BY n DESC LIMIT 20""",
            [category, from_date, to_date]
        ).fetchall()

        top_refs = c.execute(
            """SELECT referer, SUM(count) AS n FROM daily_referers
               WHERE category = ? AND day BETWEEN ? AND ?
               GROUP BY referer ORDER BY n DESC LIMIT 20""",
            [category, from_date, to_date]
        ).fetchall()

        daily = c.execute(
            """SELECT day, SUM(total_requests) FROM daily_totals
               WHERE category = ? AND day BETWEEN ? AND ?
               GROUP BY day ORDER BY day""",
            [category, from_date, to_date]
        ).fetchall()

        hourly = c.execute(
            """SELECT strftime('%Y-%m-%d %H:00', hour::TIMESTAMP), SUM(count)
               FROM hourly_totals
               WHERE category = ? AND hour >= ?::TIMESTAMP
                 AND hour < (?::TIMESTAMP + INTERVAL '1 day')
               GROUP BY hour ORDER BY hour""",
            [category, from_date, to_date]
        ).fetchall()

        countries = c.execute(
            """SELECT country_code, ANY_VALUE(country), SUM(count) AS n FROM daily_ips
               WHERE category = ? AND day BETWEEN ? AND ?
                 AND country_code IS NOT NULL AND country IS NOT NULL
               GROUP BY country_code ORDER BY n DESC LIMIT 30""",
            [category, from_date, to_date]
        ).fetchall()

        cities = c.execute(
            """SELECT city, ANY_VALUE(country_code), SUM(count) AS n FROM daily_ips
               WHERE category = ? AND day BETWEEN ? AND ?
                 AND city IS NOT NULL
               GROUP BY city ORDER BY n DESC LIMIT 30""",
            [category, from_date, to_date]
        ).fetchall()

    total_requests, bytes_total, errors, rt_sum, rt_n = totals
    avg_rt = (int(rt_sum) // int(rt_n)) if rt_n else None

    geo_info = {ip: {'country_code': cc, 'country': co, 'city': ci}
                for ip, _, cc, co, ci in top_ips if cc or co or ci}

    return {
        'total_requests': int(total_requests),
        'unique_ips': int(unique_ips or 0),
        'bytes_total': int(bytes_total),
        'errors': int(errors),
        'avg_response_time_us': avg_rt,
        'top_paths': [[p, int(n)] for p, n in top_paths],
        'all_paths': [[p, int(n)] for p, n in all_paths],
        'top_ips': [[ip, int(n)] for ip, n, *_ in top_ips],
        'status_codes': [[s, int(n)] for s, n in status_codes],
        'methods': [[m, int(n)] for m, n in methods],
        'top_user_agents': [[u, int(n)] for u, n in top_ua],
        'top_referers': [[r, int(n)] for r, n in top_refs],
        'hourly': [[h, int(n)] for h, n in hourly],
        'daily': [[str(d), int(n)] for d, n in daily],
        'geo_info': geo_info,
        'top_countries': [[cc, co, int(n)] for cc, co, n in countries],
        'top_cities': [[ci, cc, int(n)] for ci, cc, n in cities],
    }


def get_ip_geo(ip: str) -> dict:
    """Return the most recent geo data for a single IP, or {}."""
    with cursor() as c:
        row = c.execute(
            """SELECT country_code, country, city FROM daily_ips
               WHERE ip = ? AND country_code IS NOT NULL
               ORDER BY day DESC LIMIT 1""",
            [ip]
        ).fetchone()
    if not row:
        return {}
    cc, country, city = row
    return {'country_code': cc, 'country': country, 'city': city}


def query_yearly(year: int, category: str) -> dict:
    """Return yearly totals and per-month breakdown for the given year."""
    from_date = f'{year}-01-01'
    to_date = f'{year}-12-31'
    with cursor() as c:
        totals = c.execute(
            """SELECT COALESCE(SUM(total_requests),0),
                      COALESCE(SUM(bytes_total),0),
                      COALESCE(SUM(errors),0),
                      COALESCE(SUM(rt_sum),0),
                      COALESCE(SUM(rt_n),0)
               FROM daily_totals
               WHERE category = ? AND day BETWEEN ? AND ?""",
            [category, from_date, to_date]
        ).fetchone()

        unique_ips = c.execute(
            """SELECT COUNT(DISTINCT ip) FROM daily_ips
               WHERE category = ? AND day BETWEEN ? AND ?""",
            [category, from_date, to_date]
        ).fetchone()[0]

        monthly = c.execute(
            """SELECT strftime(day::TIMESTAMP, '%Y-%m') AS month,
                      SUM(total_requests) AS n
               FROM daily_totals
               WHERE category = ? AND day BETWEEN ? AND ?
               GROUP BY month ORDER BY month""",
            [category, from_date, to_date]
        ).fetchall()

    total_requests, bytes_total, errors, rt_sum, rt_n = totals
    avg_rt = (int(rt_sum) // int(rt_n)) if rt_n else None
    return {
        'year': year,
        'category': category,
        'total_requests': int(total_requests),
        'unique_ips': int(unique_ips or 0),
        'bytes_total': int(bytes_total),
        'errors': int(errors),
        'avg_response_time_us': avg_rt,
        'monthly': [[m, int(n)] for m, n in monthly],
    }


def query_available_years(category: str) -> list[int]:
    """Return distinct years that have data in daily_totals for the given category."""
    with cursor() as c:
        rows = c.execute(
            """SELECT DISTINCT CAST(EXTRACT(YEAR FROM day) AS INTEGER) AS yr
               FROM daily_totals
               WHERE category = ?
               ORDER BY yr DESC""",
            [category]
        ).fetchall()
    return [int(r[0]) for r in rows]


def drill_ip(ip: str, from_date: str, to_date: str, limit: int = 20) -> list[tuple]:
    with cursor() as c:
        return c.execute(
            """SELECT path, SUM(count) AS n FROM daily_ip_paths
               WHERE ip = ? AND day BETWEEN ? AND ?
               GROUP BY path ORDER BY n DESC LIMIT ?""",
            [ip, from_date, to_date, limit]
        ).fetchall()


def drill_path(path: str, from_date: str, to_date: str, limit: int = 20) -> list[tuple]:
    with cursor() as c:
        return c.execute(
            """SELECT ip, SUM(count) AS n FROM daily_path_ips
               WHERE path = ? AND day BETWEEN ? AND ?
               GROUP BY ip ORDER BY n DESC LIMIT ?""",
            [path, from_date, to_date, limit]
        ).fetchall()


def drill_subnets(category: str, from_date: str, to_date: str,
                  limit: int = 50) -> list[tuple]:
    """Return (ip_prefix, count, pct) for top /24 subnets in the date range.

    ip_prefix is the first three octets of the IP (e.g. '10.0.1').
    pct is the subnet's share of total traffic in the window (0–100).
    """
    with cursor() as c:
        rows = c.execute(
            """WITH subnet_counts AS (
                   SELECT
                       array_to_string(str_split(ip, '.')[1:3], '.') AS ip_prefix,
                       SUM(count) AS n
                   FROM daily_ips
                   WHERE category = ? AND day BETWEEN ? AND ?
                   GROUP BY ip_prefix
               ),
               total AS (
                   SELECT COALESCE(SUM(n), 0) AS grand_total FROM subnet_counts
               )
               SELECT s.ip_prefix, s.n,
                      CASE WHEN t.grand_total > 0
                           THEN ROUND(s.n * 100.0 / t.grand_total, 2)
                           ELSE 0.0 END AS pct
               FROM subnet_counts s, total t
               ORDER BY s.n DESC
               LIMIT ?""",
            [category, from_date, to_date, limit]
        ).fetchall()
    return rows


def drill_ip_map(from_date: str, to_date: str,
                 top_ips: int = 200, paths_per_ip: int = 20) -> dict[str, dict[str, int]]:
    """Return {ip: {path: count}} for the top-N IPs in the date range."""
    with cursor() as c:
        rows = c.execute(
            """WITH top AS (
                   SELECT ip FROM daily_ips
                   WHERE category = 'all' AND day BETWEEN ? AND ?
                   GROUP BY ip ORDER BY SUM(count) DESC LIMIT ?
               ),
               agg AS (
                   SELECT d.ip, d.path, SUM(d.count) AS n
                   FROM daily_ip_paths d
                   INNER JOIN top ON d.ip = top.ip
                   WHERE d.day BETWEEN ? AND ?
                   GROUP BY d.ip, d.path
               ),
               ranked AS (
                   SELECT ip, path, n,
                          ROW_NUMBER() OVER (PARTITION BY ip ORDER BY n DESC) AS r
                   FROM agg
               )
               SELECT ip, path, n FROM ranked WHERE r <= ?""",
            [from_date, to_date, top_ips, from_date, to_date, paths_per_ip]
        ).fetchall()
    result: dict[str, dict[str, int]] = {}
    for ip, path, n in rows:
        result.setdefault(ip, {})[path] = int(n)
    return result


def drill_path_map(from_date: str, to_date: str,
                   top_paths: int = 200, ips_per_path: int = 20) -> dict[str, dict[str, int]]:
    """Return {path: {ip: count}} for the top-N paths in the date range."""
    with cursor() as c:
        rows = c.execute(
            """WITH top AS (
                   SELECT path FROM daily_paths
                   WHERE category = 'all' AND day BETWEEN ? AND ?
                   GROUP BY path ORDER BY SUM(count) DESC LIMIT ?
               ),
               agg AS (
                   SELECT d.path, d.ip, SUM(d.count) AS n
                   FROM daily_path_ips d
                   INNER JOIN top ON d.path = top.path
                   WHERE d.day BETWEEN ? AND ?
                   GROUP BY d.path, d.ip
               ),
               ranked AS (
                   SELECT path, ip, n,
                          ROW_NUMBER() OVER (PARTITION BY path ORDER BY n DESC) AS r
                   FROM agg
               )
               SELECT path, ip, n FROM ranked WHERE r <= ?""",
            [from_date, to_date, top_paths, from_date, to_date, ips_per_path]
        ).fetchall()
    result: dict[str, dict[str, int]] = {}
    for path, ip, n in rows:
        result.setdefault(path, {})[ip] = int(n)
    return result


def reset(path: str = None) -> None:
    """Close this thread's connection and optionally switch to a new path."""
    close()
    if path:
        connect(path)


def mark_file_ingested(source: str, path: str, mtime: float, size: int) -> None:
    """Upsert a parsed_files ledger entry without touching rollup tables.

    Used to record that an archive has been fully processed using the archive
    name as the source key, so staleness checks on subsequent runs work correctly.
    """
    with cursor() as c:
        c.execute(
            """INSERT INTO parsed_files (source, path, mtime, size, rows_ingested, duration_s)
               VALUES (?, ?, ?, ?, 0, 0)
               ON CONFLICT (source) DO UPDATE SET
                   path = excluded.path,
                   mtime = excluded.mtime,
                   size = excluded.size,
                   ingested_at = now()""",
            [source, path, mtime, size],
        )


def list_ingested_files() -> list[dict]:
    with cursor() as c:
        rows = c.execute(
            """SELECT source, path, size, rows_ingested, duration_s, ingested_at
               FROM parsed_files ORDER BY ingested_at DESC"""
        ).fetchall()
    return [
        {'source': s, 'path': p, 'size': sz, 'rows': r,
         'duration_s': d, 'ingested_at': str(i)}
        for s, p, sz, r, d, i in rows
    ]
