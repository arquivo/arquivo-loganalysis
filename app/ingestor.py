"""Orchestrates log ingestion into DuckDB.

Scans a directory, diffs against the `parsed_files` ledger, runs parser workers
on only new or changed files, enriches IP rows with GeoIP in the main process,
and UPSERTs rollup rows into DuckDB.
"""
from __future__ import annotations
import logging
import os
import shutil
import tarfile
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path

from app import db, geo


@lru_cache(maxsize=200_000)
def _geo_lookup_cached(ip: str) -> tuple:
    """Module-level cache so repeated IPs across multiple files skip the mmdb lookup."""
    return geo.lookup(ip)
from app.parser import (
    _parse_file_for_db, _MP_CTX,
    _default_workers, _default_nice, _worker_init,
)

_log = logging.getLogger('ingestor')


def ingest_dir(log_dir: str, from_dt: datetime, to_dt: datetime,
               workers: int = None, progress_cb=None) -> dict:
    """Ingest every new or changed file in `log_dir`. No date-window filtering
    is applied at ingest — workers read full file content, and the 30-day
    window is enforced only at query time."""
    t0 = time.time()

    _SKIP_SUFFIXES = {'.duckdb', '.wal', '.db', '.json', '.bz2', '.zip'}

    # Separate .tar.gz archives from plain files.  A file whose last two
    # suffixes are ['.tar', '.gz'] is an archive; everything else that passes
    # the suffix blocklist is a plain text log.
    all_candidates = sorted(p for p in Path(log_dir).iterdir() if p.is_file())
    archive_files: list[Path] = []
    all_files: list[Path] = []
    for p in all_candidates:
        suffixes = [s.lower() for s in p.suffixes]
        if len(suffixes) >= 2 and suffixes[-2] == '.tar' and suffixes[-1] == '.gz':
            archive_files.append(p)
        elif p.suffix.lower() not in _SKIP_SUFFIXES:
            all_files.append(p)

    todo: list[Path] = []
    unchanged: list[str] = []
    for f in all_files:
        src = f.name
        st = f.stat()
        if db.is_file_current(src, st.st_mtime, st.st_size):
            unchanged.append(src)
        else:
            todo.append(f)

    skipped_by_name: list[str] = []  # retained for return-shape compatibility

    _log.info('Ingest: %d file(s) found; %d new/changed, %d unchanged',
              len(all_files), len(todo), len(unchanged))
    for src in unchanged:
        _log.debug('  unchanged (ledger hit): %s', src)
    for f in todo:
        _log.info('  → queued for parse: %s', f.name)

    # ── Archive handling ──────────────────────────────────────────────────────
    # For each .tar.gz that is new or changed, extract its members to a temp
    # dir and add them to the work queue.  The `source` key in the DB is
    # "archive.tar.gz/member_name" so ledger checks are tied to the archive.
    #
    # _archive_meta maps temp_file_path_str → (source_override, archive_path,
    #   archive_mtime, archive_size) so the post-worker loop can write the
    #   correct ledger entry.
    # _archive_meta maps temp_file_path_str → (source_override, archive_path,
    #   archive_mtime, archive_size) so the post-worker loop resolves the source.
    _archive_meta: dict[str, tuple[str, str, float, int]] = {}
    # _archive_members_map and _archive_info track which temp paths belong to
    # each archive so we can write a sentinel ledger entry (keyed by the archive
    # name) after all members are processed — this makes staleness checks work
    # correctly on subsequent runs.
    _archive_members_map: dict[str, list[str]] = {}  # arc_name → [member_path, ...]
    _archive_info: dict[str, tuple[str, float, int]] = {}  # arc_name → (arc_path, mtime, size)
    _temp_dirs: list[str] = []  # cleaned up in the finally block below

    for arc in sorted(archive_files):
        arc_st = arc.stat()
        arc_source_prefix = arc.name  # e.g. "access.tar.gz" — used as ledger sentinel key
        if db.is_file_current(arc_source_prefix, arc_st.st_mtime, arc_st.st_size):
            unchanged.append(arc_source_prefix)
            _log.info('  unchanged archive (ledger hit): %s', arc_source_prefix)
            continue

        _log.info('  → queued archive for extraction: %s', arc_source_prefix)
        try:
            tmpdir = tempfile.mkdtemp(prefix='loganalysis_arc_')
            _temp_dirs.append(tmpdir)
            _log.info('    extracting to: %s', tmpdir)
            with tarfile.open(str(arc), 'r:gz') as tf:
                members = [m for m in tf.getmembers() if m.isfile()]
                tf.extractall(tmpdir, members=members)
            _log.info('    extraction complete: %d member(s) in %s', len(members), tmpdir)
            _archive_info[arc_source_prefix] = (str(arc), arc_st.st_mtime, arc_st.st_size)
            _archive_members_map.setdefault(arc_source_prefix, [])
            for member in members:
                member_path = os.path.join(tmpdir, member.name)
                source_override = f'{arc_source_prefix}/{os.path.basename(member.name)}'
                _archive_meta[member_path] = (
                    source_override, str(arc), arc_st.st_mtime, arc_st.st_size)
                _archive_members_map[arc_source_prefix].append(member_path)
                _log.info('    extracted member: %s → source=%s',
                          member.name, source_override)
        except Exception as e:
            _log.exception('Failed to extract archive %s: %s', arc, e)
            skipped_by_name.append(arc_source_prefix)

    workers = max(1, workers if workers is not None else _default_workers())
    nice_inc = _default_nice()

    files_done = 0
    ingested = []
    errors = []

    def _report_progress():
        if not progress_cb:
            return
        ratio = files_done / total_files
        if ratio > 1.0:
            _log.warning('Progress ratio %.3f > 1.0 (files_done=%d, total_files=%d)',
                         ratio, files_done, total_files)
        progress_cb(min(1.0, ratio))

    # Plain file tasks: (path_str, epoch, far)
    # Archive member tasks use the same tuple shape; source is resolved via
    # _archive_meta after the worker returns.
    _EPOCH = '1970-01-01T00:00:00'
    _FAR   = '2099-12-31T23:59:59'

    all_tasks_paths: list[str] = [str(f) for f in todo] + list(_archive_meta.keys())
    # Use file count for progress so compressed and uncompressed files are
    # weighted equally and the ratio never exceeds 1.0.
    total_files = len(all_tasks_paths) or 1

    try:
        if all_tasks_paths:
            # Pass a wide open range so workers store ALL content; date filtering
            # is applied at query time in SQL, not at ingest time.
            tasks = [(p, _EPOCH, _FAR) for p in all_tasks_paths]
            with ProcessPoolExecutor(
                max_workers=workers,
                mp_context=_MP_CTX,
                initializer=_worker_init,
                initargs=(nice_inc,),
            ) as ex:
                futures = {ex.submit(_parse_file_for_db, t): t[0] for t in tasks}
                for fut in as_completed(futures):
                    fpath = futures[fut]
                    t_future_start = time.time()
                    try:
                        res = fut.result()
                    except Exception as e:
                        _log.exception('Worker crashed on %s: %s', fpath, e)
                        errors.append({'file': fpath, 'error': repr(e)})
                        files_done += 1
                        _report_progress()
                        continue
                    ipc_s = round(time.time() - t_future_start - res.get('duration_s', 0), 3)
                    if 'error' in res:
                        _log.error('Parse error in %s: %s', res['file'], res['error'])
                        errors.append(res)
                        files_done += 1
                        _report_progress()
                        continue

                    # Determine source name, stat path, mtime, and size.
                    # For archive members, override source and use archive stat.
                    res_file = res['file']
                    if res_file in _archive_meta:
                        source_override, arc_path, arc_mtime, arc_size = _archive_meta[res_file]
                        source = source_override
                        stat_path = arc_path
                        stat_mtime = arc_mtime
                        stat_size = arc_size
                    else:
                        source = os.path.basename(res_file)
                        st = Path(res_file).stat()
                        stat_path = res_file
                        stat_mtime = st.st_mtime
                        stat_size = st.st_size

                    t_enrich_start = time.time()
                    _enrich_geo(res['tables'])
                    t_enrich = time.time() - t_enrich_start

                    try:
                        t_write_start = time.time()
                        db.replace_file_rollups(
                            source, stat_path, stat_mtime, stat_size,
                            res['tables'], res['rows'], res['duration_s'])
                        t_write = time.time() - t_write_start
                    except Exception as e:
                        _log.exception('DB write failed for %s: %s', source, e)
                        errors.append({'file': res_file, 'error': repr(e)})
                        res = None
                        files_done += 1
                        _report_progress()
                        continue

                    files_done += 1
                    rows_logged = res['rows']
                    bytes_logged = res['bytes_read']
                    duration_logged = res['duration_s']
                    res = None  # release parsed tables before next future
                    _report_progress()
                    ingested.append(source)
                    total_logged = duration_logged + ipc_s + t_enrich + t_write
                    _log.info(
                        '✓ %s  parse=%.2fs  ipc=%.3fs  enrich=%.2fs  write=%.2fs  total=%.2fs'
                        '  |  %s rows  %.1f MB',
                        source, duration_logged, ipc_s, t_enrich, t_write, total_logged,
                        f"{rows_logged:,}", bytes_logged / 1e6)
    finally:
        # Always clean up temp extraction dirs, even on failure.
        for tmpdir in _temp_dirs:
            try:
                shutil.rmtree(tmpdir)
                _log.info('Cleaned up temp dir: %s', tmpdir)
            except Exception as e:
                _log.warning('Failed to remove temp dir %s: %s', tmpdir, e)

    # Write archive-level sentinel ledger entries so that subsequent runs can
    # detect unchanged archives via is_file_current(archive_name, mtime, size).
    # Only written when every member of the archive was processed without error.
    error_files = {e['file'] for e in errors}
    for arc_name, members in _archive_members_map.items():
        if members and not any(m in error_files for m in members):
            arc_path, arc_mtime, arc_size = _archive_info[arc_name]
            try:
                db.mark_file_ingested(arc_name, arc_path, arc_mtime, arc_size)
                _log.info('  sentinel written for archive: %s', arc_name)
            except Exception as e:
                _log.warning('Failed to write archive sentinel for %s: %s', arc_name, e)

    db.enforce_retention()

    return {
        'files_ingested': ingested,
        'files_unchanged': unchanged,
        'files_skipped': skipped_by_name,
        'errors': errors,
        'workers_used': workers,
        'worker_nice': nice_inc,
        'duration_s': round(time.time() - t0, 2),
    }


def _enrich_geo(tables: dict) -> None:
    """Expand daily_ips rows from 4 columns to 7, filling geo when available."""
    rows = tables.get('daily_ips') or []
    if not rows:
        return
    available = geo.is_available()
    _log.info('Enriching %d IPs with GeoIP data (available=%s)', len(rows), available)
    new_rows = []
    for r in rows:
        # Worker emitted: (day, category, ip, count)
        day, cat, ip, count = r
        cc, country, city = _geo_lookup_cached(ip) if available else (None, None, None)
        new_rows.append((day, cat, ip, count, cc, country, city))
    tables['daily_ips'] = new_rows
