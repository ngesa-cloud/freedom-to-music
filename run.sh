#!/usr/bin/env bash
# ==============================================================================
# 🎵 b33t — Freedom to Music (Desktop Edition Launcher)
# ==============================================================================
set -e

PORT="${PORT:-8080}"
echo "=================================================="
echo " 🎵 b33t — Freedom to Music"
echo " Zero Subscriptions • Zero Ads • 100% Open & Fast"
echo "=================================================="

# Ensure Python 3 is installed
if ! command -v python3 &>/dev/null; then
  echo "[-] Error: python3 is required to run b33t."
  exit 1
fi

# Optional local virtualenv
if [ -d ".venv" ]; then
  source .venv/bin/activate
elif [ -d "venv" ]; then
  source venv/bin/activate
fi

# Start the server
echo "[*] Launching b33t on http://localhost:${PORT}..."
exec python3 server.py
