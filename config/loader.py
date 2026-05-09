"""YAML configuration loader."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_DEFAULT_PATH = Path(__file__).parent / "settings.yaml"
_cached_settings: dict[str, Any] | None = None


def load_settings(path: str | Path | None = None) -> dict[str, Any]:
    """Load and cache settings from YAML config file."""
    global _cached_settings
    if _cached_settings is not None and path is None:
        return _cached_settings

    config_path = Path(path) if path else _DEFAULT_PATH
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with open(config_path, encoding="utf-8") as f:
        settings = yaml.safe_load(f)

    if path is None:
        _cached_settings = settings
    return settings


def get_fmp_settings() -> dict[str, Any]:
    """Get FMP-specific settings."""
    return load_settings().get("fmp", {})


def get_r2_settings() -> dict[str, Any]:
    """Get R2-specific settings."""
    return load_settings().get("r2", {})


def get_pipeline_settings() -> dict[str, Any]:
    """Get pipeline-specific settings."""
    return load_settings().get("pipeline", {})


def get_evolution_settings() -> dict[str, Any]:
    """Get evolution-specific settings."""
    return load_settings().get("evolution", {})
