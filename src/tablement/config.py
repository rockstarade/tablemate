"""YAML configuration loading and validation."""

from pathlib import Path

import yaml

from tablement.errors import ConfigError
from tablement.models import SnipeConfig


def load_snipe_config(path: str | Path) -> SnipeConfig:
    """Load and validate a snipe config from a YAML file."""
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")

    try:
        with open(path) as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise ConfigError(f"Invalid YAML: {e}") from e

    if not isinstance(data, dict):
        raise ConfigError(f"Config file must contain a YAML mapping, got {type(data).__name__}")

    try:
        return SnipeConfig.model_validate(data)
    except Exception as e:
        raise ConfigError(f"Invalid config: {e}") from e
