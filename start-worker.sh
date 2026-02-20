#!/bin/bash
# Start the Celery worker with auto-restart on failure

cd "$(dirname "$0")"

while true; do
    echo "[$(date)] Starting Celery worker..."
    ./venv/bin/python -m celery -A src.tasks.base:celery_app worker \
        --include=src.tasks.pm_agent,src.tasks.dev_agent,src.tasks.qa_agent,src.tasks.tech_lead_agent,src.tasks.stall_checker \
        --queues=backend \
        --loglevel=info \
        --concurrency=4 \
        --hostname=backend-worker@%h \
        2>&1 | tee -a celery_worker.log
    
    EXIT_CODE=$?
    echo "[$(date)] Worker exited with code $EXIT_CODE. Restarting in 5 seconds..."
    sleep 5
done
