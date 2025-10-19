#!/bin/bash

source .venv/bin/activate

while true; do
    python3 run_api.py
    echo "Script crashed with exit code $?. Restarting..." >&2
    sleep 1  # Optional: pause before restarting
done
