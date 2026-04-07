#!/bin/bash
set -e

cd "$(dirname "$0")"

# Create venv if missing
if [ ! -f ".venv/bin/python" ]; then
    echo "[setup] Creating virtual environment..."
    python3 -m venv .venv
fi

# Install / sync dependencies
echo "[setup] Installing dependencies..."
.venv/bin/pip install -q -r requirements.txt

# Check .env exists
if [ ! -f ".env" ]; then
    echo "[error] .env file not found. Please copy .env.example and fill in your credentials:"
    echo "  cp .env.example .env"
    exit 1
fi

echo "[start] Starting email approval bot..."
exec .venv/bin/python main.py
