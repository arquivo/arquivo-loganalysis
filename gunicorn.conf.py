import multiprocessing
import os

# One worker is mandatory: the app holds in-process _state and runs a single
# watchdog + parse thread. Multiple workers would conflict on DuckDB writes
# and see inconsistent _state across requests.
workers = 1

# Scale threads with available CPUs; override with GUNICORN_THREADS env var.
threads = int(os.environ.get("GUNICORN_THREADS", multiprocessing.cpu_count() * 2 + 1))

bind = "0.0.0.0:5000"
timeout = 300
accesslog = "-"
errorlog = "-"
