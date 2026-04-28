import sys, os
# Prevent app.app from launching background parse/watchdog threads during tests.
os.environ.setdefault('APP_NO_AUTOSTART', '1')
sys.path.insert(0, os.path.dirname(__file__))
