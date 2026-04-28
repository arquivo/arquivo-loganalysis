"""GeoIP lookups using an optional MaxMind GeoLite2 City database.

Mount a valid ``.mmdb`` file at ``/app/geoip/GeoLite2-City.mmdb`` (or set
``GEOIP_DB``). If no file is present, the app runs with geo features disabled.
Get a free copy at https://dev.maxmind.com/geoip/geolite2-free-geolocation-data.
"""
import os
import logging

_log = logging.getLogger('geo')
_reader = None
_attempted = False


def _reader_instance():
    global _reader, _attempted
    if _attempted:
        return _reader
    _attempted = True
    path = os.environ.get('GEOIP_DB', '/app/geoip/GeoLite2-City.mmdb')
    if not os.path.exists(path):
        _log.info('GeoIP DB not found at %s — geo features disabled', path)
        return None
    try:
        import geoip2.database  # type: ignore
        _reader = geoip2.database.Reader(path)
        _log.info('GeoIP DB loaded from %s', path)
    except ImportError:
        _log.warning('geoip2 library not installed; geo disabled')
    except Exception as exc:  # pragma: no cover
        _log.warning('Failed to open GeoIP DB: %s', exc)
    return _reader


def lookup(ip: str):
    """Return ``(country_code, country_name, city)`` or ``(None, None, None)``."""
    r = _reader_instance()
    if not r:
        return None, None, None
    try:
        resp = r.city(ip)
        return (
            resp.country.iso_code,
            resp.country.name,
            resp.city.name,
        )
    except Exception:
        return None, None, None


def is_available() -> bool:
    return _reader_instance() is not None


def reset():
    """Force re-detection of the DB (used by tests)."""
    global _reader, _attempted
    _reader = None
    _attempted = False
