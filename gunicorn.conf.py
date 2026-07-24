# gunicorn.conf.py
bind = "0.0.0.0:8001"  # or your preferred port
workers = 4
worker_class = "uvicorn.workers.UvicornWorker"  # Since you're using FastAPI
timeout = 120

# ===== ADD THIS LOGGING CONFIGURATION =====
# Log to stdout/stderr so journalctl can capture
accesslog = "-"
errorlog = "-"
loglevel = "info"

# For even more verbose output (shows all print statements)
# loglevel = "debug"

# Capture stdout/stderr from workers
capture_output = True
enable_stdio_inheritance = True