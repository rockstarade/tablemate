#!/usr/bin/env bash
#
# Tablement EC2 Setup Script
# Run this ONCE on a fresh Ubuntu EC2 instance (t3.micro, us-east-1).
#
# Why us-east-1? Resy's API goes through Imperva CDN with an edge PoP in
# Ashburn, VA. EC2 in us-east-1 → Imperva Ashburn → Resy origin = ~5-15ms RTT.
# That's 10-50x faster than residential connections.
#
# Usage:
#   ssh ubuntu@<EC2-IP>
#   git clone <your-repo> ~/tablement
#   cd ~/tablement
#   bash deploy/setup-ec2.sh
#
# After running, you MUST:
#   1. Copy your .env file:  scp .env ubuntu@<EC2-IP>:~/tablement/.env
#   2. Start the service:    sudo systemctl start tablement
#
set -euo pipefail

echo "========================================="
echo "  Tablement EC2 Setup"
echo "========================================="

# System packages
echo "[1/7] Installing system dependencies..."
sudo apt-get update -qq
sudo apt-get install -y -qq python3.12 python3.12-venv python3-pip git curl chrony

# If python3.12 isn't available (older Ubuntu), try python3.11 or python3
PYTHON=$(command -v python3.12 || command -v python3.11 || command -v python3)
echo "Using Python: $PYTHON ($($PYTHON --version))"

# ---- AMAZON TIME SYNC (critical for snipe timing) ----
# EC2 instances have access to Amazon Time Sync at 169.254.169.123
# This gives sub-microsecond accuracy via the Precision Hardware Clock (PHC).
# Default NTP gives ~10-50ms jitter. Amazon Time Sync gives <1ms.
# For a sniper that needs to fire within 30ms of the drop, this is essential.
echo "[2/7] Configuring Amazon Time Sync (sub-microsecond accuracy)..."

# Configure chrony to use Amazon Time Sync as primary source
sudo tee /etc/chrony/chrony.conf > /dev/null << 'CHRONYCONF'
# Amazon Time Sync — sub-microsecond accuracy via PHC
# This is a local link-local address, zero network latency
server 169.254.169.123 prefer iburst minpoll 4 maxpoll 4

# Fallback public NTP pools (only used if Amazon Time Sync is down)
pool ntp.ubuntu.com iburst maxsources 2
pool 0.ubuntu.pool.ntp.org iburst maxsources 1

# Allow the system clock to be stepped on startup (faster initial sync)
makestep 1.0 3

# Record tracking data for monitoring
driftfile /var/lib/chrony/drift
logdir /var/log/chrony

# Minimize jitter: use aggressive polling (2^4 = 16 second intervals)
# and allow fine-tuning of the system clock
rtcsync
CHRONYCONF

sudo systemctl restart chrony
sudo systemctl enable chrony

# Wait for sync
sleep 2
echo "  Time sync status:"
chronyc tracking 2>/dev/null | grep -E "System time|Last offset|RMS offset" || echo "  (chrony still syncing — will be ready in ~30s)"

# Create virtual environment
echo "[3/7] Creating virtual environment..."
cd /home/ubuntu/tablement
$PYTHON -m venv .venv
source .venv/bin/activate

# Install tablement
echo "[4/7] Installing tablement..."
pip install --upgrade pip setuptools wheel -q
pip install -e ".[dev]" -q 2>/dev/null || pip install -e . -q

# Verify install
echo "[5/7] Verifying installation..."
tablement --help > /dev/null 2>&1 && echo "  tablement CLI: OK" || echo "  WARNING: tablement CLI not found"
python -c "from tablement.web.app import create_app; print('  FastAPI app: OK')"

# Install systemd service
echo "[6/7] Installing systemd service..."
sudo cp deploy/tablement.service /etc/systemd/system/tablement.service
sudo systemctl daemon-reload
sudo systemctl enable tablement

# Verify network to Resy
echo "[7/7] Testing network to Resy API..."
RESY_LATENCY=$(curl -sI -o /dev/null -w "%{time_total}" "https://api.resy.com/3/venue?id=25973" \
    -H 'Authorization: ResyAPI api_key="VbWk7s3L4KiK5fzlO7JD3Q5EYolJI7n5"' 2>/dev/null || echo "failed")
echo "  Resy API round-trip: ${RESY_LATENCY}s"

# Check if Imperva cookies are being set
IMPERVA_CHECK=$(curl -sI "https://api.resy.com/3/venue?id=25973" \
    -H 'Authorization: ResyAPI api_key="VbWk7s3L4KiK5fzlO7JD3Q5EYolJI7n5"' 2>/dev/null \
    | grep -c "incap_ses\|visid_incap\|nlbi_" || echo "0")
echo "  Imperva cookies received: ${IMPERVA_CHECK}"

echo ""
echo "========================================="
echo "  Setup Complete!"
echo "========================================="
echo ""
echo "Next steps:"
echo "  1. Copy your .env file to this server:"
echo "     scp .env ubuntu@<this-server>:~/tablement/.env"
echo ""
echo "  2. Start the service:"
echo "     sudo systemctl start tablement"
echo ""
echo "  3. Check status:"
echo "     sudo systemctl status tablement"
echo "     journalctl -u tablement -f"
echo ""
echo "  4. Check clock sync (should show <1ms offset):"
echo "     chronyc tracking"
echo ""
echo "  5. Access the admin panel:"
echo "     http://<this-server>:8422/admin/vip"
echo ""
echo "  SECURITY GROUP: Allow inbound TCP 8422 from your IP!"
echo ""
echo "  EC2 RECOMMENDATION:"
echo "    Instance: t3.micro (or t3.small if you need more RAM)"
echo "    Region:   us-east-1 (closest to Resy/Imperva edge)"
echo "    AMI:      Ubuntu 24.04 LTS"
echo "    Storage:  8 GB gp3"
echo ""
