"""User settings in `~/.loccit/settings.toml`."""

import json
import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Settings:
    # Folder with the built web frontend (its `dist`), served by the backend.
    frontend_dist: Path | None = None


def load_settings(path: Path) -> Settings:
    if not path.is_file():
        return Settings()
    with path.open("rb") as file:
        table = tomllib.load(file)
    frontend_dist = table.get("frontend_dist")
    return Settings(frontend_dist=Path(frontend_dist).expanduser() if frontend_dist else None)


def save_settings(path: Path, settings: Settings) -> None:
    lines = ["# loccit settings\n"]
    if settings.frontend_dist:
        # A JSON string is a valid TOML basic string.
        lines.append(f"frontend_dist = {json.dumps(str(settings.frontend_dist))}\n")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(lines), encoding="utf-8")
