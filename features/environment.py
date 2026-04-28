import os
import sys
import tempfile
import shutil

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# Prevent app.app from launching background threads and opening the
# production DB when the module is first imported during behave runs.
os.environ.setdefault('APP_NO_AUTOSTART', '1')

# Point the DB at a writable temp path so the module-level db.connect()
# call in app.app succeeds without touching the production analytics.duckdb.
_behave_tmp = tempfile.mkdtemp(prefix='behave_db_')
os.environ.setdefault('DB_PATH', os.path.join(_behave_tmp, 'behave.duckdb'))


def before_scenario(context, scenario):
    context.tmpdir = tempfile.mkdtemp()
    context.cache_dir = os.path.join(context.tmpdir, 'cache')
    os.makedirs(context.cache_dir, exist_ok=True)


def after_scenario(context, scenario):
    shutil.rmtree(context.tmpdir, ignore_errors=True)
    for var in ('PARSE_WORKERS', 'PARSE_NICE', 'REPARSE_COOLDOWN'):
        os.environ.pop(var, None)
    # Reset cooldown constant to default so it doesn't leak between scenarios
    try:
        import app.db as _db
        _db._REPARSE_COOLDOWN_S = 300
    except Exception:
        pass
