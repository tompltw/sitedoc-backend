#!/bin/bash
# Start the Celery Beat scheduler with auto-restart on failure

cd "$(dirname "$0")"

while true; do
    echo "[$(date)] Starting Celery Beat..."
    ./venv/bin/python -m celery -A src.tasks.base:celery_app beat \
        --loglevel=info \
        2>&1 | tee -a celery_beat.log

    EXIT_CODE=$?
    echo "[$(date)] Beat exited with code $EXIT_CODE. Restarting in 5 seconds..."
    sleep 5
done
