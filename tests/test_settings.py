from pathlib import Path

from loccit.settings import Settings, load_settings, save_settings


def test_missing_file_gives_defaults(tmp_path):
    assert load_settings(tmp_path / "settings.toml") == Settings(frontend_dist=None)


def test_round_trip(tmp_path):
    file = tmp_path / "sub" / "settings.toml"
    dist = Path('/Users/me/My "Projects"/loccit-web-frontend/dist')
    save_settings(file, Settings(frontend_dist=dist))
    assert load_settings(file) == Settings(frontend_dist=dist)
    save_settings(file, Settings())
    assert load_settings(file) == Settings()
