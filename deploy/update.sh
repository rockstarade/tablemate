#!/usr/bin/env bash
#
# Quick redeploy: pull latest code, reinstall, restart service.
#
# Usage:
#   ssh ubuntu@<EC2-IP>
#   cd ~/tablement && bash deploy/update.sh
#
set -euo pipefail

echo "Updating Tablement..."

cd /home/ubuntu/tablement

# Pull latest
echo "[1/3] Pulling latest code..."
git pull --ff-only

# Reinstall (picks up new dependencies)
echo "[2/3] Reinstalling..."
source .venv/bin/activate
pip install -e . -q

# Restart service
echo "[3/3] Restarting service..."
sudo systemctl restart tablement

echo ""
echo "Done! Checking status..."
sleep 2
sudo systemctl status tablement --no-pager -l
echo ""
echo "Logs: journalctl -u tablement -f"
