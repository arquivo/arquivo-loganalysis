"""Session-level test configuration.

Sets env vars at import time — before any test module can trigger
`app.app`'s module-level ``db.connect(DB_PATH)`` — so tests never
touch the production analytics.duckdb.
"""
import os
import tempfile

os.environ.setdefault('APP_NO_AUTOSTART', '1')

_tmp = tempfile.mkdtemp(prefix='pytest_db_')
os.environ.setdefault('DB_PATH', os.path.join(_tmp, 'test.duckdb'))
