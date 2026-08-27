#!/bin/bash
# start.sh — Start WireGuard VPN (if configured), then launch the bot.
#
# If /app/config/wg0.conf exists, bring up the WireGuard tunnel BEFORE
# starting the bot.  This routes ALL outbound traffic from this container
# through the VPN — the VPS host, SSH, Dokploy, and other projects are
# completely unaffected.
#
# If wg0.conf does not exist, the bot starts without VPN (direct connection).

set -e

# --- VPN Setup ---
WG_CONF="/etc/wireguard/wg0.conf"
if [ ! -f "$WG_CONF" ]; then
    WG_CONF="/app/config/wg0.conf"
fi
if [ -f "$WG_CONF" ]; then
    echo "[start.sh] WireGuard config found — bringing up VPN tunnel..."
    cp "$WG_CONF" /etc/wireguard/wg0.conf
    chmod 600 /etc/wireguard/wg0.conf

    # WireGuard needs NET_ADMIN capability (add to Docker/Dokploy config)
    wg-quick up wg0

    # Wait for tunnel to establish
    sleep 3

    # Verify VPN is working
    IP=$(curl -s --max-time 10 https://ipinfo.io/country 2>/dev/null || echo "??")
    echo "[start.sh] VPN connected — outbound IP country: $IP"

    if [ "$IP" = "??" ]; then
        echo "[start.sh] WARNING: Could not verify VPN. Continuing anyway..."
    fi
else
    echo "[start.sh] No wg0.conf found — starting bot without VPN (direct connection)"
fi

# --- Start Bot ---
echo "[start.sh] Starting Quad bot..."
exec python -m quad "$@"
