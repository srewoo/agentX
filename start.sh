#!/bin/bash
# Start StockPilot backend

set -e

cd "$(dirname "$0")/backend"

# Create virtualenv if not exists
if [ ! -d ".venv" ]; then
  echo "Creating Python virtual environment..."
  python3 -m venv .venv
fi

source .venv/bin/activate

# Install dependencies
echo "Installing dependencies..."
pip install -r requirements.txt -q

# Copy .env.example if .env not present
if [ ! -f ".env" ]; then
  cp .env.example .env
  echo ""
  echo "⚠️  Created .env from .env.example"
  echo "   Add your LLM API keys to backend/.env before using AI features"
  echo ""
fi

echo "Starting StockPilot backend at http://localhost:8000 ..."
echo "API docs: http://localhost:8020/docs"
python run.py
