"""Elastic IP pool for multi-IP outbound connections.

Discovers all local private IPs on the EC2 instance. Each IP is mapped to a
unique public Elastic IP by AWS VPC NAT. Scouts and snipes bind to different
local IPs so their traffic exits from different public IPs.

On a dev machine or single-IP EC2, auto-discovers 1 IP and everything shares it.
On multi-IP EC2, distributes scouts/snipes across available IPs.

No configuration needed — auto-discovers at import time.
"""

from __future__ import annotations

import logging
import socket
import subprocess

logger = logging.getLogger(__name__)


class IPPool:
    """Manages local IP assignment for outbound connections.

    Each scout or snipe gets a sticky local IP. httpx binds to this via
    ``local_address`` on ``AsyncHTTPTransport``, so outbound traffic exits
    through the corresponding public Elastic IP.

    Usage::

        ip = ip_pool.get_ip("scout-drop-123")   # sticky assignment
        client = ResyApiClient(local_address=ip)
        ...
        ip_pool.release("scout-drop-123")        # free for reuse
    """

    def __init__(self) -> None:
        self._ips: list[str] = []
        self._assignments: dict[str, str] = {}  # key → local IP
        self._discover()

    def _discover(self) -> None:
        """Auto-detect all usable local IPs (excludes loopback, docker, etc.)."""
        # Method 1: hostname -I (Linux, fast)
        try:
            result = subprocess.run(
                ["hostname", "-I"], capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                self._ips = [
                    ip.strip()
                    for ip in result.stdout.split()
                    if ip.strip()
                    and not ip.startswith("127.")
                    and not ip.startswith("172.17.")  # docker bridge
                    and not ip.startswith("fe80:")  # ipv6 link-local
                    and ":" not in ip  # skip all ipv6
                ]
        except Exception:
            pass

        # Method 2: socket fallback (Mac / single-IP systems)
        if not self._ips:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.connect(("8.8.8.8", 80))
                self._ips = [s.getsockname()[0]]
                s.close()
            except Exception:
                self._ips = ["0.0.0.0"]

        logger.info("IP pool: %d IPs available — %s", len(self._ips), self._ips)
        if len(self._ips) <= 1:
            logger.warning(
                "ONLY 1 IP AVAILABLE — all scouts/snipes will share one IP! "
                "Add secondary private IPs + EIPs to EC2 for IP distribution. "
                "See deploy docs for setup instructions."
            )

    @property
    def count(self) -> int:
        """Number of available local IPs."""
        return len(self._ips)

    @property
    def ips(self) -> list[str]:
        """List of available local IPs."""
        return list(self._ips)

    def get_ip(self, key: str) -> str:
        """Get a local IP for a key (scout ID or user ID).

        Sticky: same key always returns the same IP (until released).
        Least-used: new keys get the IP with the fewest current assignments.
        """
        if key in self._assignments:
            return self._assignments[key]

        # Pick the least-used IP
        used_counts: dict[str, int] = {ip: 0 for ip in self._ips}
        for assigned_ip in self._assignments.values():
            if assigned_ip in used_counts:
                used_counts[assigned_ip] += 1

        best_ip = min(self._ips, key=lambda ip: used_counts[ip])
        self._assignments[key] = best_ip
        logger.debug("Assigned IP %s to %s", best_ip, key[:30])
        return best_ip

    def release(self, key: str) -> None:
        """Release an IP assignment (scout stopped or snipe completed).

        The actual EIP stays on EC2 — this only frees the logical assignment
        so another scout/snipe can use that IP slot.
        """
        removed = self._assignments.pop(key, None)
        if removed:
            logger.debug("Released IP %s from %s", removed, key[:30])

    def get_stats(self) -> dict:
        """Return pool statistics for monitoring/admin."""
        per_ip: dict[str, int] = {ip: 0 for ip in self._ips}
        for assigned_ip in self._assignments.values():
            if assigned_ip in per_ip:
                per_ip[assigned_ip] += 1

        return {
            "total_ips": len(self._ips),
            "ips": self._ips,
            "active_assignments": len(self._assignments),
            "per_ip": per_ip,
        }


# Module-level singleton — auto-discovers IPs at import time
ip_pool = IPPool()
