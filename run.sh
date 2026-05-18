#!/bin/bash
set -e

cd "$(dirname "$0")/backend"

if [ ! -d ".venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv .venv
fi

source .venv/bin/activate

echo "Installing dependencies..."
pip install -q -r requirements.txt

export SYNC_CONFIG="${SYNC_CONFIG:-../config.json}"

echo "Starting Proxmox-CloudStack Sync on http://0.0.0.0:8088"
python main.py
