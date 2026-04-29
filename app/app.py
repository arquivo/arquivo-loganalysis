import os
import time
import threading
import logging
from datetime import datetime
from flask import Flask, jsonify, render_template, request, make_response
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

from app.logging_config import setup as setup_logging, get_recent_logs

setup_logging()
_log = logging.getLogger('app')

from app.parser import parse_logs   # noqa: E402
from app import db                   # noqa: E402

LOG_DIR = os.path.abspath(os.environ.get(
    'LOG_DIR', os.path.join(os.path.dirname(__file__), '..', 'httpd')))
WINDOW_DAYS = int(os.environ.get('WINDOW_DAYS', '30'))
DB_PATH = os.environ.get(
    'DB_PATH', os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'data', 'analytics.duckdb')))
SUBNET_ALERT_PCT = float(os.environ.get('SUBNET_ALERT_PCT', '20'))

# Export DB_PATH so parse_logs (and ingestor) pick it up via env.
os.environ['DB_PATH'] = DB_PATH

app = Flask(__name__)

_state = {
    'status': 'idle',       # idle | parsing | done | error
    'progress': 0.0,
    'message': '',
    'last_updated': None,
    'from_date': None,
    'to_date': None,
}
_lock = threading.Lock()
_debounce_timer = None


def _run_parse(from_date=None, to_date=None):
    def on_progress(pct):
        with _lock:
            _state['progress'] = min(100.0, round(pct * 100, 1))
            _state['message'] = f'Parsing… {_state["progress"]}%'

    try:
        _log.info('Parse thread starting (from=%s, to=%s)', from_date, to_date)
        # parse_logs now handles ingest + DB writes; result is only used for
        # metadata (files_parsed, duration_s, etc.) — charts query DB directly.
        parse_logs(LOG_DIR, days=WINDOW_DAYS,
                   from_date=from_date, to_date=to_date,
                   progress_cb=on_progress, db_path=DB_PATH)
        with _lock:
            _state['status'] = 'done'
            _state['progress'] = 100.0
            _state['message'] = 'Done'
            _state['last_updated'] = time.time()
        _log.info('Parse thread done')
    except Exception as exc:
        _log.exception('Parse failed: %s', exc)
        with _lock:
            _state['status'] = 'error'
            _state['message'] = str(exc)


def start_parsing(from_date=None, to_date=None, force=False):
    with _lock:
        if _state['status'] == 'parsing':
            _log.debug('Parse already in progress; ignoring start request')
            return

    # Fast-path: when no custom range is requested and the ledger already
    # covers every file on disk, skip the parse thread entirely. The dashboard
    # endpoints query DuckDB directly on demand, so we don't need to "parse"
    # just to surface existing data — that was the UX bug where every Docker
    # restart looked like a full re-ingest.
    if not force and from_date is None and to_date is None:
        try:
            from app.ingestor import has_pending_work
            if not has_pending_work(LOG_DIR):
                with _lock:
                    _state['status'] = 'done'
                    _state['progress'] = 100.0
                    _state['message'] = 'Up to date — no parsing needed'
                    if _state['last_updated'] is None:
                        _state['last_updated'] = time.time()
                _log.info('Ledger covers every file in %s; skipping parse', LOG_DIR)
                return
        except Exception:
            _log.exception('Pending-work check failed; falling back to full parse')

    with _lock:
        _state['status'] = 'parsing'  # hold the slot before releasing the lock
        _state['progress'] = 0.0
        _state['message'] = 'Parsing logs…'
        _state['from_date'] = from_date
        _state['to_date'] = to_date
    threading.Thread(target=_run_parse, args=(from_date, to_date), daemon=True).start()


class _LogHandler(FileSystemEventHandler):
    def on_modified(self, event):
        if not event.is_directory:
            _log.info('Watchdog: modified %s', event.src_path)
            self._schedule()

    def on_created(self, event):
        if not event.is_directory:
            _log.info('Watchdog: created %s', event.src_path)
            self._schedule()

    def _schedule(self):
        global _debounce_timer
        if _debounce_timer:
            _debounce_timer.cancel()
        _debounce_timer = threading.Timer(3.0, start_parsing)
        _debounce_timer.start()


def _start_watcher():
    if not os.path.isdir(LOG_DIR):
        _log.warning('LOG_DIR does not exist: %s — watcher NOT started', LOG_DIR)
        return
    try:
        observer = Observer()
        observer.schedule(_LogHandler(), LOG_DIR, recursive=False)
        observer.daemon = True
        observer.start()
        _log.info('Watchdog started on %s', LOG_DIR)
    except Exception as e:
        _log.exception('Failed to start watcher: %s', e)


def _current_range():
    """Return (from_date, to_date) strings for the current parse window."""
    with _lock:
        fd = _state.get('from_date')
        td = _state.get('to_date')
    from datetime import datetime, timedelta
    if not fd:
        fd = (datetime.now() - timedelta(days=WINDOW_DAYS)).strftime('%Y-%m-%d')
    if not td:
        td = datetime.now().strftime('%Y-%m-%d')
    return fd, td


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    start_parsing()
    return render_template('index.html')


@app.route('/api/status')
def api_status():
    with _lock:
        return jsonify({
            'status': _state['status'],
            'progress': _state['progress'],
            'message': _state['message'],
            'last_updated': _state['last_updated'],
            'from_date': _state['from_date'],
            'to_date': _state['to_date'],
        })


@app.route('/api/stats')
def api_stats():
    category = request.args.get('category', 'all')
    if category not in ('all', 'human', 'bot'):
        return jsonify({'error': 'invalid category'}), 400

    from_date = request.args.get('from_date') or None
    to_date   = request.args.get('to_date')   or None
    if not from_date or not to_date:
        from_date, to_date = _current_range()
    if from_date and to_date and from_date > to_date:
        return jsonify({'error': 'invalid date range: from_date must not be after to_date'}), 400
    try:
        stats = db.query_stats(category, from_date, to_date)
    except Exception as exc:
        _log.exception('query_stats failed: %s', exc)
        return jsonify({'error': str(exc)}), 500

    stats['category'] = category
    stats['from_date'] = from_date
    stats['to_date'] = to_date
    stats['window_days'] = WINDOW_DAYS
    stats['geo_available'] = stats.get('geo_info') is not None

    # Decorate top_ips with geo columns the UI expects: [ip, n, cc, country]
    geo_info = stats.pop('geo_info', {}) or {}
    stats['top_ips'] = [
        [ip, n,
         (geo_info.get(ip) or {}).get('country_code'),
         (geo_info.get(ip) or {}).get('country')]
        for ip, n in stats['top_ips']
    ]

    # Attach ingest metadata from parsed_files ledger
    files = db.list_ingested_files()
    stats['files_parsed'] = [f['source'] for f in files]
    stats['files_skipped'] = []

    return jsonify(stats)


@app.route('/api/years')
def api_years():
    category = request.args.get('category', 'all')
    if category not in ('all', 'human', 'bot'):
        return jsonify({'error': 'invalid category'}), 400
    try:
        years = db.query_available_years(category)
    except Exception as exc:
        _log.exception('query_available_years failed: %s', exc)
        return jsonify({'error': str(exc)}), 500
    return jsonify({'years': years})


@app.route('/api/yearly')
def api_yearly():
    year_str = request.args.get('year', '')
    category = request.args.get('category', 'all')

    if category not in ('all', 'human', 'bot'):
        return jsonify({'error': 'invalid category'}), 400

    try:
        year = int(year_str)
        if year < 1970 or year > 9999:
            raise ValueError('year out of range')
    except (ValueError, TypeError):
        return jsonify({'error': 'invalid year: must be a 4-digit integer'}), 400

    try:
        stats = db.query_yearly(year, category)
    except Exception as exc:
        _log.exception('query_yearly failed: %s', exc)
        return jsonify({'error': str(exc)}), 500

    return jsonify(stats)


@app.route('/api/yearly/download')
def api_yearly_download():
    year_str = request.args.get('year', '')
    category = request.args.get('category', 'all')

    if category not in ('all', 'human', 'bot'):
        return jsonify({'error': 'invalid category'}), 400

    try:
        year = int(year_str)
        if year < 1970 or year > 9999:
            raise ValueError('year out of range')
    except (ValueError, TypeError):
        return jsonify({'error': 'invalid year: must be a 4-digit integer'}), 400

    try:
        stats = db.query_yearly(year, category)
    except Exception as exc:
        _log.exception('query_yearly failed: %s', exc)
        return jsonify({'error': str(exc)}), 500

    try:
        from weasyprint import HTML as WeasyprintHTML
    except ImportError:
        return jsonify({'error': 'weasyprint is not installed'}), 500

    # ── Helper to format bytes nicely ─────────────────────────────────────────
    def fmt_bytes(b):
        b = b or 0
        if b >= 1e12:
            return f'{b/1e12:.2f} TB'
        if b >= 1e9:
            return f'{b/1e9:.2f} GB'
        if b >= 1e6:
            return f'{b/1e6:.2f} MB'
        if b >= 1e3:
            return f'{b/1e3:.2f} KB'
        return f'{b} B'

    category_label = {'all': 'All Traffic', 'human': 'Human', 'bot': 'Bot'}.get(category, category)
    avg_rt = (f"{stats['avg_response_time_us']/1000:.1f} ms"
              if stats.get('avg_response_time_us') else 'N/A')
    generated_at = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')

    # ── Monthly bar chart (pure HTML/CSS) ────────────────────────────────────
    monthly = stats.get('monthly', [])
    max_count = max((n for _, n in monthly), default=1) or 1

    month_rows = ''
    for month_str, count in monthly:
        bar_pct = round(count / max_count * 100)
        month_rows += f'''
        <tr>
          <td style="padding:4px 8px;width:90px;font-size:13px;color:#374151">{month_str}</td>
          <td style="padding:4px 8px;width:100px;font-size:13px;text-align:right;color:#374151">
            {count:,}
          </td>
          <td style="padding:4px 8px">
            <div style="background:#e5e7eb;border-radius:3px;height:14px;width:300px">
              <div style="background:#3b82f6;border-radius:3px;height:14px;width:{bar_pct}%"></div>
            </div>
          </td>
        </tr>'''

    # ── Monthly breakdown table ───────────────────────────────────────────────
    monthly_table_rows = ''
    for month_str, count in monthly:
        monthly_table_rows += f'''
        <tr>
          <td style="padding:6px 12px;border-bottom:1px solid #e5e7eb;font-size:13px">{month_str}</td>
          <td style="padding:6px 12px;border-bottom:1px solid #e5e7eb;font-size:13px;text-align:right">{count:,}</td>
        </tr>'''

    html = f'''<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <style>
    @page {{
      margin: 2cm 2cm 2.5cm 2cm;
      @bottom-center {{
        content: "Page " counter(page) " of " counter(pages);
        font-size: 10px;
        color: #9ca3af;
      }}
    }}
    body {{
      font-family: "Helvetica Neue", Helvetica, Arial, sans-serif;
      color: #1f2937;
      background: #ffffff;
      margin: 0;
      padding: 0;
    }}
    h1 {{ margin: 0 0 4px 0; font-size: 24px; font-weight: 700; color: #ffffff; }}
    h2 {{ font-size: 16px; font-weight: 600; color: #1e3a5f; margin: 24px 0 10px 0; }}
    .header {{
      background: #1e3a5f;
      color: #ffffff;
      padding: 28px 32px 22px 32px;
      border-radius: 6px 6px 0 0;
    }}
    .header-sub {{ font-size: 13px; color: #bfdbfe; margin-top: 4px; }}
    .body-wrap {{ padding: 24px 32px; }}
    .cards {{
      display: flex;
      gap: 12px;
      margin-bottom: 28px;
      flex-wrap: wrap;
    }}
    .card {{
      flex: 1;
      min-width: 120px;
      border: 1px solid #e5e7eb;
      border-radius: 6px;
      padding: 14px 16px;
      background: #f9fafb;
    }}
    .card-label {{ font-size: 11px; color: #6b7280; text-transform: uppercase; letter-spacing: .05em; margin-bottom: 4px; }}
    .card-value {{ font-size: 20px; font-weight: 700; color: #1e3a5f; }}
    .card.accent {{ border-left: 4px solid #3b82f6; }}
    table.monthly {{
      width: 100%;
      border-collapse: collapse;
      margin-bottom: 28px;
    }}
    table.monthly thead th {{
      background: #1e3a5f;
      color: #ffffff;
      padding: 8px 12px;
      font-size: 12px;
      text-align: left;
      font-weight: 600;
    }}
    table.monthly thead th:last-child {{ text-align: right; }}
    table.monthly tbody tr:nth-child(even) {{ background: #f9fafb; }}
    .section-title {{
      font-size: 14px;
      font-weight: 600;
      color: #1e3a5f;
      border-bottom: 2px solid #3b82f6;
      padding-bottom: 6px;
      margin: 24px 0 12px 0;
    }}
    .footer {{
      margin-top: 32px;
      padding-top: 12px;
      border-top: 1px solid #e5e7eb;
      font-size: 11px;
      color: #9ca3af;
      text-align: center;
    }}
  </style>
</head>
<body>
  <div class="header">
    <h1>Annual Traffic Report &mdash; {year}</h1>
    <div class="header-sub">Category: {category_label} &nbsp;|&nbsp; Generated: {generated_at}</div>
  </div>

  <div class="body-wrap">
    <div class="section-title">Summary</div>
    <div class="cards">
      <div class="card accent">
        <div class="card-label">Total Requests</div>
        <div class="card-value">{stats["total_requests"]:,}</div>
      </div>
      <div class="card accent">
        <div class="card-label">Unique IPs</div>
        <div class="card-value">{stats["unique_ips"]:,}</div>
      </div>
      <div class="card accent">
        <div class="card-label">Data Transferred</div>
        <div class="card-value">{fmt_bytes(stats["bytes_total"])}</div>
      </div>
      <div class="card accent">
        <div class="card-label">Errors</div>
        <div class="card-value">{stats["errors"]:,}</div>
      </div>
      <div class="card accent">
        <div class="card-label">Avg Response Time</div>
        <div class="card-value">{avg_rt}</div>
      </div>
    </div>

    <div class="section-title">Monthly Breakdown</div>
    <table class="monthly">
      <thead>
        <tr>
          <th>Month</th>
          <th style="text-align:right">Requests</th>
        </tr>
      </thead>
      <tbody>{monthly_table_rows if monthly_table_rows else '<tr><td colspan="2" style="padding:8px 12px;color:#6b7280">No data for this year.</td></tr>'}
      </tbody>
    </table>

    <div class="section-title">Monthly Traffic Chart</div>
    <table style="border-collapse:collapse;width:100%;margin-bottom:28px">
      <tbody>{month_rows if month_rows else '<tr><td style="padding:8px;color:#6b7280">No data.</td></tr>'}
      </tbody>
    </table>

    <div class="footer">
      Annual Traffic Report &mdash; {year} &mdash; {category_label}<br>
      Generated on {generated_at}
    </div>
  </div>
</body>
</html>'''

    try:
        pdf_bytes = WeasyprintHTML(string=html).write_pdf()
    except Exception as exc:
        _log.exception('PDF generation failed: %s', exc)
        return jsonify({'error': f'PDF generation failed: {exc}'}), 500

    filename = f'traffic_report_{year}_{category}.pdf'
    response = make_response(pdf_bytes)
    response.headers['Content-Type'] = 'application/pdf'
    response.headers['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response


@app.route('/api/drill/ip')
def api_drill_ip():
    ip = request.args.get('ip', '')
    from_date, to_date = _current_range()
    paths = db.drill_ip(ip, from_date, to_date)
    geo = db.get_ip_geo(ip)
    return jsonify({
        'ip': ip,
        'geo': geo,
        'total_requests': sum(n for _, n in paths),
        'paths': [[p, n] for p, n in paths],
    })


@app.route('/api/drill/path')
def api_drill_path():
    path = request.args.get('path', '')
    from_date, to_date = _current_range()
    ips = db.drill_path(path, from_date, to_date)
    return jsonify({
        'path': path,
        'total_requests': sum(n for _, n in ips),
        'ips': [[ip, n, None, None] for ip, n in ips],
    })


@app.route('/api/drill/subnets')
def api_drill_subnets():
    category = request.args.get('category', 'all')
    if category not in ('all', 'human', 'bot'):
        return jsonify({'error': 'invalid category'}), 400
    from_date = request.args.get('from_date') or None
    to_date   = request.args.get('to_date')   or None
    if not from_date or not to_date:
        from_date, to_date = _current_range()
    rows = db.drill_subnets(category, from_date, to_date)
    subnets = [
        {
            'ip_prefix': prefix,
            'count': int(count),
            'pct': float(pct),
            'is_alert': float(pct) >= SUBNET_ALERT_PCT,
        }
        for prefix, count, pct in rows
    ]
    return jsonify({'subnets': subnets, 'alert_pct': SUBNET_ALERT_PCT})


@app.route('/api/logs')
def api_logs():
    n = min(int(request.args.get('n', 200)), 1000)
    level = request.args.get('level')
    logs = get_recent_logs(n)
    if level:
        logs = [l for l in logs if l['level'] == level.upper()]
    return jsonify({'logs': logs})


@app.route('/api/refresh', methods=['POST'])
def api_refresh():
    data = request.get_json(silent=True) or {}
    from_date = data.get('from_date') or request.args.get('from_date')
    to_date = data.get('to_date') or request.args.get('to_date')
    with _lock:
        if _state['status'] == 'parsing':
            _log.debug('Refresh requested but parse already in progress; ignoring')
            return jsonify({'status': 'already parsing',
                            'from_date': from_date, 'to_date': to_date}), 409
    _log.info('Manual refresh requested (from=%s, to=%s)', from_date, to_date)
    start_parsing(from_date=from_date, to_date=to_date)
    return jsonify({'status': 'parsing started',
                    'from_date': from_date, 'to_date': to_date})


db.connect(DB_PATH)
if not os.environ.get('APP_NO_AUTOSTART'):
    _start_watcher()
    start_parsing()


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
