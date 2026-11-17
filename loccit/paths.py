"""Locations of everything loccit stores. All of it lives under one folder."""

from pathlib import Path


def data_dir() -> Path:
    return Path.home() / ".loccit"


def overrides_file(root: Path | None = None) -> Path:
    return (root or data_dir()) / "overrides.toml"


def settings_file(root: Path | None = None) -> Path:
    return (root or data_dir()) / "settings.toml"


def models_file(root: Path | None = None) -> Path:
    return (root or data_dir()) / "models.toml"


def database_file(root: Path | None = None) -> Path:
    return (root or data_dir()) / "loccit.sqlite"


def documents_dir(root: Path | None = None) -> Path:
    return (root or data_dir()) / "documents"
