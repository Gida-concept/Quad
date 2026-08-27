#!/bin/bash
# start.sh — Launch the Quad bot.
#
# Environment variables (optionally set in docker-compose.yml or host shell):
#   HTTP_PROXY  / HTTPS_PROXY  — Proxy server for HTTP/WebSocket traffic
#
# No VPN or special network privileges required.

set -e

# --- Start Bot ---
echo "[start.sh] Starting Quad bot..."
exec python -m quad "$@"
