# Apache Log Analyser

Flask web application that ingests Apache HTTP server access logs, stores daily rollups in DuckDB, and serves a live traffic analytics dashboard.

---

## Table of Contents

1. [Features](#features)
2. [Quick Start](#quick-start)
3. [Dashboard](#dashboard)
4. [Configuration](#configuration)
5. [Log Format](#log-format)
6. [Supported File Types](#supported-file-types)
7. [GeoIP Enrichment](#geoip-enrichment)
8. [API Reference](#api-reference)
9. [Architecture](#architecture)
10. [Development](#development)

---

## Features

- **Multi-process parsing** — worker pool scales to `os.cpu_count()` by default; workers run at a reduced OS priority so the host stays responsive during heavy ingestion.
- **DuckDB rollup store** — files are ingested once (tracked by `mtime` + `size`); all dashboard queries hit pre-aggregated tables instead of scanning raw logs.
- **Bot/human split** — UA heuristic separates automated clients; dashboard toggles between All / Human / Bot views.
- **Date-range picker** — any range within the stored data can be analysed, independent of the `WINDOW_DAYS` default.
- **Live updates** — `watchdog` observer triggers a re-parse whenever the log directory changes; the dashboard refreshes automatically.
- **Drill-down** — click any IP or path to see its related paths or IPs (top 500 × top 20 retained per day).
- **Subnet concentration detection** — `/api/drill/subnets` groups traffic by `/24` prefix and flags subnets that exceed a configurable share threshold.
- **GeoIP enrichment** — optional MaxMind GeoLite2 enrichment adds country/city data; top-countries bar chart, top-cities table, and country flags on IP rows.
- **Yearly PDF report** — formatted annual report downloadable from the dashboard or via API.
- **Month navigation** — selecting a year reveals Jan–Dec buttons to scope the view to a single month.
- **Live logs panel** — the app's own log stream is shown inside the dashboard, filterable by level.
- **Compressed log support** — plain `.gz` files and `.tar.gz` archives are ingested transparently alongside plain text logs.
- **Configurable retention** — rows older than `RETENTION_DAYS` (default 3 years) are automatically deleted.

---

## Quick Start

### Docker (recommended)

```bash
# Place Apache log files in ./httpd, then:
docker compose up --build
```

Open http://localhost:5000 — parsing starts in the background immediately.

### Local

```bash
uv venv
uv pip install -r requirements.txt
LOG_DIR=./httpd DB_PATH=./data/analytics.duckdb .venv/bin/python -m app.app
```

---

## Dashboard

The dashboard is a single-page app that polls `/api/status` and loads data from `/api/stats`.

| Panel | Content |
|-------|---------|
| Summary cards | Total requests, unique IPs, bytes transferred, average response time |
| Daily traffic chart | Line chart of daily request totals |
| Status code donut | Distribution of HTTP status codes |
| Method donut | GET/POST/HEAD/etc. breakdown |
| Top 10 paths | Bar chart of most-visited paths |
| All paths | Top 1 000 paths with live search |
| Top IPs | Top 50 IPs, optionally with country flags |
| Top countries | Bar chart (requires GeoIP) |
| Top cities | Table (requires GeoIP) |
| Top referers | Top 20 referring URLs |
| Top user agents | Top 20 UA strings |
| Subnet drill-down | Top /24 subnets, alert flag for high-concentration ranges |
| Download PDF | Annual report for the selected year |
| Live logs | Scrollable, filterable stream of the app's own log output |

---

## Configuration

All settings are controlled via environment variables.

### Core

| Variable | Default | Description |
|----------|---------|-------------|
| `LOG_DIR` | `./httpd` | Directory containing Apache log files to ingest. |
| `DB_PATH` | `./data/analytics.duckdb` | Path to the DuckDB analytics file. |
| `WINDOW_DAYS` | `30` | Default rolling query window (days). Applied at query time only — full file content is always stored. |
| `RETENTION_DAYS` | `1095` | Rows older than this number of days are automatically deleted (3 years). |
| `LOG_LEVEL` | `INFO` | Root logger level: `DEBUG`, `INFO`, `WARNING`, `ERROR`. |
| `APP_NO_AUTOSTART` | _(unset)_ | Set to `1` to suppress the watchdog and background parse on startup (used by tests). |

### Parsing & Workers

| Variable | Default | Description |
|----------|---------|-------------|
| `PARSE_WORKERS` | `os.cpu_count()` | Maximum number of worker processes for parallel log ingestion. |
| `PARSE_NICE` | `10` | POSIX nice increment applied to worker processes (ignored on Windows). Higher values mean lower priority. |
| `REPARSE_COOLDOWN` | `300` | Seconds that must pass after a file was last ingested before it is eligible for re-ingest on change. Suppresses thrashing on actively-written log files. |

### GeoIP

| Variable | Default | Description |
|----------|---------|-------------|
| `GEOIP_DB` | `/app/geoip/GeoLite2-City.mmdb` | Path to the MaxMind GeoLite2-City database. Geo features are disabled silently if the file is absent. |

### Production (Gunicorn)

| Variable | Default | Description |
|----------|---------|-------------|
| `GUNICORN_THREADS` | `cpu_count × 2 + 1` | Threads per Gunicorn worker (HTTP request concurrency). **Note:** Gunicorn must be kept at 1 worker — the app maintains in-process state and DuckDB connections that are incompatible with multiple workers. |

### Dashboard Behaviour

| Variable | Default | Description |
|----------|---------|-------------|
| `SUBNET_ALERT_PCT` | `20` | Subnets whose share of total requests meets or exceeds this percentage are flagged `is_alert: true` in `/api/drill/subnets`. |

### Advanced: Drill-Down & Cardinality Caps

These control how much data is retained in memory during parsing and stored in the rollup tables. The defaults are suitable for most deployments.

| Variable | Default | Description |
|----------|---------|-------------|
| `DRILL_IPS_PER_DAY` | `500` | Top N IPs retained per day for drill-down queries. |
| `DRILL_PATHS_PER_IP` | `20` | Top N paths stored per IP per day. |
| `DRILL_PATHS_PER_DAY` | `500` | Top N paths retained per day for drill-down queries. |
| `DRILL_IPS_PER_PATH` | `20` | Top N IPs stored per path per day. |
| `MAX_UA_PER_BUCKET` | `5000` | Max unique user-agent strings tracked per (day, category) bucket. |
| `MAX_REF_PER_BUCKET` | `5000` | Max unique referer strings tracked per (day, category) bucket. |
| `MAX_PATHS_PER_BUCKET` | `50000` | Max unique paths tracked per (day, category) bucket. |
| `MAX_IPS_PER_BUCKET` | `50000` | Max unique IPs tracked per (day, category) bucket. |

Once a cap is reached for a bucket, new entries are dropped but existing entries continue to accumulate counts accurately.

---

## Log Format

Apache Combined Log Format with an optional trailing microsecond response time field:

```
%h %l %u %t "%r" %>s %b "%{Referer}i" "%{User-Agent}i" [%D]
```

Example line:

```
203.0.113.42 - - [01/Apr/2025:12:34:56 +0000] "GET /index.html HTTP/1.1" 200 4321 "https://example.com" "Mozilla/5.0 ..." 342
```

All non-binary files in `LOG_DIR` are considered for ingestion — there is no filename filter. Date filtering is applied only at query time via `WHERE day BETWEEN ? AND ?`.

---

## Supported File Types

| Type | Detection | Handling |
|------|-----------|----------|
| Plain text log | Any file not matching other rules | Read directly with 8 MB buffer |
| Gzip-compressed log (`.gz`) | File suffix `.gz` (but not `.tar.gz`) | Decompressed transparently by the parser worker |
| Tar+gzip archive (`.tar.gz`) | Suffix pair `.tar.gz` | Extracted to a temp dir; each member file is parsed independently; source name stored as `archive.tar.gz/member_name` |
| Skipped | `.duckdb`, `.wal`, `.db`, `.json`, `.bz2`, `.zip` | Ignored entirely |

A file is re-ingested only when its `(mtime, size)` differ from the ledger **and** it has not been ingested within the last `REPARSE_COOLDOWN` seconds.

---

## GeoIP Enrichment

GeoIP is optional. Without it the app runs fully with no geo features shown.

**Setup:**

1. Sign up for a free [MaxMind account](https://www.maxmind.com/en/geolite2/signup).
2. Download `GeoLite2-City.mmdb`.
3. Place it at `./geoip/GeoLite2-City.mmdb` (or at the path specified by `GEOIP_DB`).
4. Restart — logs will confirm `GeoIP DB loaded from …`.

**What it adds:**
- Country code and name on all IP rows.
- City name (where available).
- Top-countries bar chart in the dashboard.
- Top-cities table in the dashboard.
- Country flags next to IPs in the Top IPs panel.
- Geo data returned by `/api/drill/ip`.

---

## API Reference

All endpoints return JSON unless noted. Dates are ISO format strings (`YYYY-MM-DD`). Category must be one of `all`, `human`, or `bot`.

---

### `GET /api/status`

Current parse state.

**Response**

```json
{
  "status": "parsing",
  "progress": 42.0,
  "message": "Parsing… 42.0%",
  "last_updated": 1745000000.0,
  "from_date": "2025-03-26",
  "to_date": "2025-04-25"
}
```

| Field | Type | Notes |
|-------|------|-------|
| `status` | string | `idle` / `parsing` / `done` / `error` |
| `progress` | float | 0.0 – 100.0; only meaningful during `parsing` |
| `message` | string | Human-readable status |
| `last_updated` | float\|null | Unix timestamp of last successful parse completion |
| `from_date` | string\|null | Active date range start |
| `to_date` | string\|null | Active date range end |

---

### `GET /api/stats`

Full traffic statistics for the given date range.

**Query params**

| Param | Default | Notes |
|-------|---------|-------|
| `category` | `all` | `all`, `human`, or `bot` |
| `from_date` | `today − WINDOW_DAYS` | ISO date, inclusive |
| `to_date` | today | ISO date, inclusive |

**Response** (abbreviated)

```json
{
  "category": "all",
  "from_date": "2025-03-26",
  "to_date": "2025-04-25",
  "total_requests": 123456,
  "unique_ips": 4321,
  "bytes_total": 9876543210,
  "errors": 12,
  "avg_response_time_us": 340,
  "top_paths":     [["path", count], ...],
  "all_paths":     [["path", count], ...],
  "top_ips":       [["ip", count, "CC", "Country"], ...],
  "status_codes":  [["200", count], ...],
  "methods":       [["GET", count], ...],
  "top_user_agents": [["UA string", count], ...],
  "top_referers":  [["https://...", count], ...],
  "daily":         [["2025-04-24", count], ...],
  "hourly":        [["2025-04-25T14:00:00", count], ...],
  "top_countries": [["CC", "Country", count], ...],
  "top_cities":    [["City", "CC", count], ...],
  "geo_available": true
}
```

Returns `202 Accepted` with `{"status": "parsing"}` while a parse is in progress and the DB has no data yet.

---

### `GET /api/years`

Calendar years that have traffic data in the store.

**Query params:** `category` (default `all`)

**Response**

```json
{"years": [2025, 2024, 2023]}
```

---

### `GET /api/yearly`

Yearly totals and monthly breakdown for a specific year.

**Query params:** `year` (4-digit integer), `category` (default `all`)

**Response**

```json
{
  "year": 2025,
  "category": "all",
  "total_requests": 1234567,
  "unique_ips": 9876,
  "bytes_total": 98765432100,
  "errors": 42,
  "avg_response_time_us": 310,
  "monthly": [
    ["2025-01", 100000],
    ["2025-02", 95000],
    ...
  ]
}
```

---

### `GET /api/yearly/download`

Download an annual traffic report as a PDF.

**Query params:** `year` (4-digit integer), `category` (default `all`)

**Response:** `application/pdf` file.  
**Filename:** `traffic_report_{year}_{category}.pdf`

The PDF includes a title page, five summary metric cards, a monthly breakdown table, and a monthly bar chart.

---

### `GET /api/drill/ip`

Top paths accessed by a specific IP.

**Query params:** `ip` (IP address string)

**Response**

```json
{
  "ip": "203.0.113.42",
  "geo": {"country_code": "PT", "country": "Portugal", "city": "Lisbon"},
  "total_requests": 412,
  "paths": [["/index.html", 200], ["/about", 100], ...]
}
```

Returns up to 20 paths, sorted by count descending.

---

### `GET /api/drill/path`

Top IPs that accessed a specific path.

**Query params:** `path` (URL path string)

**Response**

```json
{
  "path": "/index.html",
  "total_requests": 8900,
  "ips": [["203.0.113.42", 200, "PT", "Portugal"], ...]
}
```

Returns up to 20 IPs, sorted by count descending.

---

### `GET /api/drill/subnets`

Top `/24` subnets by request volume, with an alert flag for subnets exceeding the concentration threshold.

**Query params:** `category` (default `all`), `from_date`, `to_date`

**Response**

```json
{
  "subnets": [
    {"ip_prefix": "203.0.113", "count": 4500, "pct": 36.5, "is_alert": true},
    {"ip_prefix": "198.51.100", "count": 1200, "pct": 9.7, "is_alert": false},
    ...
  ],
  "alert_pct": 20.0
}
```

Returns up to 50 subnets. `is_alert` is `true` when `pct >= SUBNET_ALERT_PCT`.

---

### `GET /api/logs`

Recent entries from the application's in-memory log ring buffer.

**Query params**

| Param | Default | Notes |
|-------|---------|-------|
| `n` | `200` | Number of entries (max 1000) |
| `level` | _(all)_ | Filter by level: `DEBUG`, `INFO`, `WARNING`, `ERROR` |

**Response**

```json
{
  "logs": [
    {"ts": 1745000000.123, "level": "INFO", "logger": "ingestor", "message": "✓ access.log  parse=0.21s ..."},
    ...
  ]
}
```

---

### `POST /api/refresh`

Invalidates the current parse state and starts a new parse, optionally with a custom date range.

**Request body (JSON, optional)**

```json
{"from_date": "2025-01-01", "to_date": "2025-04-25"}
```

Both fields are optional; omitting them uses the default `WINDOW_DAYS` rolling window.

**Response**

```json
{"status": "parsing started", "from_date": "2025-01-01", "to_date": "2025-04-25"}
```

---

## Architecture

### Request Flow

```
Startup
  db.connect(DB_PATH)
  _start_watcher()  →  watchdog monitors LOG_DIR
  start_parsing()   →  background thread

Background Thread
  parse_logs()
    ingest_dir()
      ├─ scan LOG_DIR, diff against parsed_files ledger
      ├─ extract .tar.gz archives to temp dirs
      ├─ ProcessPoolExecutor: _parse_file_for_db() × N workers
      │    each worker: stream file → per-day counters → row tuples
      ├─ main process: _enrich_geo() (GeoIP lookup + LRU cache)
      └─ db.replace_file_rollups() per file (atomic: delete + insert)
    db.enforce_retention()
    _state['status'] = 'done'

HTTP Requests (concurrent via Gunicorn threads)
  /api/stats  →  db.query_stats()   →  JSON
  /api/drill  →  db.drill_*()       →  JSON
  /api/status →  _state dict        →  JSON
```

### DuckDB Schema

Ten rollup tables keyed on `(day, category, source)`, plus the `parsed_files` ingest ledger:

| Table | Key columns | Content |
|-------|-------------|---------|
| `daily_totals` | day, category, source | total requests, bytes, errors, response-time sum/count |
| `daily_paths` | day, category, source, path | request count per path |
| `daily_ips` | day, category, source, ip | request count per IP + geo columns |
| `daily_status` | day, category, source, status | request count per HTTP status code |
| `daily_methods` | day, category, source, method | request count per HTTP method |
| `daily_ua` | day, category, source, user_agent | request count per user-agent string |
| `daily_referers` | day, category, source, referer | request count per referer |
| `hourly_totals` | hour (TIMESTAMP), category, source | hourly request count |
| `daily_ip_paths` | day, source, ip, path | top 500 IPs × top 20 paths per day |
| `daily_path_ips` | day, source, path, ip | top 500 paths × top 20 IPs per day |
| `parsed_files` | source (PK) | ingest ledger: mtime, size, rows, duration |

`source` is the log file basename (e.g., `access.log`) or archive member path (e.g., `logs.tar.gz/access.log`).

**Date filtering is SQL-only.** Workers parse full file content; the `WINDOW_DAYS` window is applied only in `WHERE day BETWEEN ? AND ?` queries. This means re-querying with a different range shows the correct historical data without re-parsing files.

### Thread and Process Model

- **1 Gunicorn worker** (required — the app holds in-process state and thread-local DuckDB connections).
- **N Gunicorn threads** (configurable via `GUNICORN_THREADS`) handle HTTP concurrency.
- **1 background parse thread** (daemon) runs `parse_logs()`.
- **N worker processes** (configurable via `PARSE_WORKERS`) run `_parse_file_for_db()` in parallel. Workers use the `spawn` multiprocessing context to avoid fork-in-threaded-process deadlocks.
- All DuckDB connections are **thread-local**: each thread opens its own connection to the same file.

### Bot Detection

User-agents are classified as bots via a case-insensitive regex match against a curated list of known bot, crawler, and monitoring UA patterns. Empty or `-` user-agents are also treated as bots. Three traffic views are maintained in parallel:

| Category | Contains |
|----------|----------|
| `all` | Human + bot traffic combined |
| `human` | Requests whose UA did not match any bot pattern |
| `bot` | Requests whose UA matched a bot pattern or was empty |

---

## Development

### Prerequisites

- Python 3.11+
- [`uv`](https://github.com/astral-sh/uv) for virtual environment management

### Setup

```bash
uv venv
uv pip install -r requirements.txt
```

### Running locally

```bash
LOG_DIR=./httpd DB_PATH=./data/analytics.duckdb .venv/bin/python -m app.app
```

### Tests

```bash
.venv/bin/pytest tests/ -v                         # 69 unit tests
.venv/bin/pytest tests/test_parser.py -v           # parser tests only
.venv/bin/pytest tests/test_app.py::TestStats -v   # single class
.venv/bin/pytest -k "test_bot_split" -v            # single test by name

.venv/bin/behave features/                         # 40 BDD scenarios
.venv/bin/behave features/log_parsing.feature      # single feature file
```

All new features follow the BDD workflow: a failing `.feature` scenario is written first, then the implementation is added to make it pass. `behave features/` passing is part of the definition of done alongside `pytest`.
