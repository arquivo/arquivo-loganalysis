"""Centralised logging setup + in-memory ring buffer exposed via /api/logs."""
import logging
import os
import sys
import collections
import threading

_RING_SIZE = 1000
_ring = collections.deque(maxlen=_RING_SIZE)
_lock = threading.Lock()


class _RingHandler(logging.Handler):
    """Stashes each log record in an in-memory deque for the UI."""

    def emit(self, record):
        try:
            entry = {
                'ts': record.created,
                'level': record.levelname,
                'logger': record.name,
                'message': record.getMessage(),
            }
            with _lock:
                _ring.append(entry)
        except Exception:
            pass  # never let logging break the app


def get_recent_logs(n: int = 200):
    with _lock:
        return list(_ring)[-n:]


_initialised = False


def setup():
    global _initialised
    if _initialised:
        return
    _initialised = True

    level = os.environ.get('LOG_LEVEL', 'INFO').upper()
    fmt = logging.Formatter(
        '%(asctime)s %(levelname)-5s [%(name)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    )

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)

    ring = _RingHandler()
    ring.setFormatter(fmt)

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)
    root.addHandler(stream)
    root.addHandler(ring)

    # tone down the noisy ones
    logging.getLogger('werkzeug').setLevel(logging.WARNING)
    logging.getLogger('watchdog').setLevel(logging.WARNING)

    logging.getLogger('logging_config').info('Logging initialised at level=%s', level)
