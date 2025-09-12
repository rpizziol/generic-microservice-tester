#!/bin/sh
# This script acts as the container's entrypoint.
# It reads environment variables to configure Gunicorn, providing sane defaults.

# Read the GUNICORN_WORKERS variable, defaulting to 2 if not set.
WORKERS=${GUNICORN_WORKERS:-2}

# Read the GUNICORN_THREADS variable, defaulting to 4 if not set.
THREADS=${GUNICORN_THREADS:-4}

echo "Starting Gunicorn with ${WORKERS} workers and ${THREADS} threads."

# Execute Gunicorn.
# The 'exec' command replaces the shell process with the Gunicorn process.
# --access-logfile - : Tells Gunicorn to write access logs to stdout.
exec gunicorn --workers ${WORKERS} --threads ${THREADS} --bind 0.0.0.0:8080 --access-logfile - app:app

