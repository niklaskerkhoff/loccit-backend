"""Locations of everything loccit stores. All of it lives under one folder."""

from pathlib import Path


def data_dir() -> Path:
    return Path.home() / ".loccit"


def overrides_file() -> Path:
    return data_dir() / "overrides.toml"
