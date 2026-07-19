# gunicorn.conf.py
bind = "0.0.0.0:8001"  # or your preferred port
workers = 4
worker_class = "uvicorn.workers.UvicornWorker"  # Since you're using FastAPI
timeout = 120