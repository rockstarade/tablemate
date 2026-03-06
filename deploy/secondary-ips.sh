#!/usr/bin/env bash
#
# Add secondary private IPs to the network interface.
# These IPs are assigned in the AWS console (ENI) and each maps to an EIP,
# but the OS needs to know about them to bind outgoing connections.
#
# ip_pool.py auto-discovers these via `hostname -I`.
#
# EIP Mapping (us-east-1):
#   54.163.10.37   → 172.31.2.90   (primary — already configured by AWS)
#   100.55.162.7   → 172.31.13.95  (secondary)
#   34.227.5.120   → 172.31.11.190 (secondary)
#   44.206.191.161 → 172.31.12.212 (secondary)
#
set -euo pipefail

# Detect the main network interface (ens5 on t3.*, eth0 on older types)
IFACE=$(ip -o -4 route show to default | awk '{print $5}' | head -1)
if [ -z "$IFACE" ]; then
    echo "ERROR: Could not detect network interface" >&2
    exit 1
fi

echo "Using interface: $IFACE"

# Secondary private IPs (assigned in AWS console → ENI → Manage IP addresses)
SECONDARY_IPS=(
    "172.31.13.95/20"
    "172.31.11.190/20"
    "172.31.12.212/20"
)

for IP in "${SECONDARY_IPS[@]}"; do
    if ip addr show dev "$IFACE" | grep -q "${IP%/*}"; then
        echo "  $IP already present on $IFACE — skipping"
    else
        ip addr add "$IP" dev "$IFACE" || echo "  WARNING: failed to add $IP"
        echo "  Added $IP to $IFACE"
    fi
done

echo "Active IPs: $(hostname -I)"
