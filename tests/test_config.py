"""Tests for config loading and validation."""

import tempfile
from pathlib import Path

import pytest

from tablement.config import load_snipe_config
from tablement.errors import ConfigError


def _write_yaml(content: str) -> str:
    """Write YAML to a temp file and return the path."""
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False)
    f.write(content)
    f.close()
    return f.name


class TestLoadSnipeConfig:
    def test_valid_config(self):
        path = _write_yaml("""
venue_id: 12345
venue_name: "Semma"
party_size: 2
date: "2026-03-26"
time_preferences:
  - time: "19:00"
    seating_type: "Dining Room"
  - time: "19:30"
drop_time:
  hour: 10
  minute: 0
  timezone: "America/New_York"
  days_ahead: 30
""")
        config = load_snipe_config(path)

        assert config.venue_id == 12345
        assert config.venue_name == "Semma"
        assert config.party_size == 2
        assert str(config.date) == "2026-03-26"
        assert len(config.time_preferences) == 2
        assert config.time_preferences[0].seating_type == "Dining Room"
        assert config.time_preferences[1].seating_type is None
        assert config.drop_time.hour == 10
        assert config.drop_time.days_ahead == 30

    def test_defaults_applied(self):
        path = _write_yaml("""
venue_id: 12345
party_size: 2
date: "2026-03-26"
time_preferences:
  - time: "19:00"
drop_time:
  hour: 10
  minute: 0
""")
        config = load_snipe_config(path)

        # Check defaults
        assert config.retry.duration_seconds == 10.0
        assert config.retry.interval_seconds == 0.0
        assert config.retry.max_attempts == 200
        assert config.drop_time.timezone == "America/New_York"
        assert config.drop_time.days_ahead == 30
        assert config.drop_time.second == 0

    def test_missing_venue_id(self):
        path = _write_yaml("""
party_size: 2
date: "2026-03-26"
time_preferences:
  - time: "19:00"
drop_time:
  hour: 10
  minute: 0
""")
        with pytest.raises(ConfigError, match="Invalid config"):
            load_snipe_config(path)

    def test_missing_time_preferences(self):
        path = _write_yaml("""
venue_id: 12345
party_size: 2
date: "2026-03-26"
drop_time:
  hour: 10
  minute: 0
""")
        with pytest.raises(ConfigError, match="Invalid config"):
            load_snipe_config(path)

    def test_invalid_party_size(self):
        path = _write_yaml("""
venue_id: 12345
party_size: 0
date: "2026-03-26"
time_preferences:
  - time: "19:00"
drop_time:
  hour: 10
  minute: 0
""")
        with pytest.raises(ConfigError, match="Invalid config"):
            load_snipe_config(path)

    def test_file_not_found(self):
        with pytest.raises(ConfigError, match="not found"):
            load_snipe_config("/nonexistent/path.yaml")

    def test_invalid_yaml(self):
        path = _write_yaml("not: [valid: yaml: {{")
        with pytest.raises(ConfigError):
            load_snipe_config(path)
