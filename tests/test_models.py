import pytest

from loccit import models
from loccit.models import ModelSettingsError, load_model_settings, parse_model_settings
from loccit.strategies import Usage


@pytest.fixture
def settings(tmp_path):
    return load_model_settings(tmp_path / "models.toml")


def test_defaults_are_written_on_first_use_and_can_be_edited(tmp_path):
    file = tmp_path / "home" / "models.toml"
    settings = load_model_settings(file)
    assert settings.default_model == "claude-sonnet-5-5"
    assert [model.id for model in settings.models] == [
        "claude-sonnet-5-5",
        "claude-opus-5-5",
        "claude-haiku-5-5",
        "claude-fable-5-2",
    ]
    file.write_text(file.read_text().replace('"claude-fable-5-2"', '"claude-fable-6"'))
    assert load_model_settings(file).models[-1].id == "claude-fable-6"


def test_off_is_the_default_level_only_where_thinking_can_be_turned_off(settings):
    assert settings.get("claude-sonnet-5-5").levels[0] == "off"
    assert settings.get("claude-haiku-5-5").levels[0] == "off"
    # Thinking is always on for these; the lowest effort comes first instead.
    assert settings.get("claude-opus-5-5").levels == ("low", "medium", "high", "xhigh", "max")
    assert settings.get("claude-fable-5-2").levels == ("low", "medium", "high", "xhigh", "max")


def test_request_options_per_level(settings):
    sonnet = settings.get("claude-sonnet-5-5")
    assert sonnet.request_options("off") == {
        "thinking": {"type": "between_tools"},
        "output_config": {"effort": "low"},
    }
    assert sonnet.request_options("high") == {
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": "high"},
    }
    assert settings.get("claude-haiku-5-5").request_options("off")["thinking"] == {
        "type": "disabled"
    }
    with pytest.raises(ModelSettingsError):
        settings.get("claude-opus-5-5").request_options("off")


def test_cost_counts_cached_tokens_at_their_own_price(settings):
    sonnet = settings.get("claude-sonnet-5-5")
    assert sonnet.cost(Usage(input_tokens=1_000_000)) == pytest.approx(2.00)
    assert sonnet.cost(Usage(output_tokens=1_000_000)) == pytest.approx(10.00)
    assert sonnet.cost(Usage(cache_read_tokens=1_000_000)) == pytest.approx(0.10)
    assert sonnet.cost(Usage(cache_write_tokens=1_000_000)) == pytest.approx(2.50)
    usage = Usage(input_tokens=200, output_tokens=150, cache_read_tokens=18_000)
    assert sonnet.cost(usage) == pytest.approx((200 * 2 + 150 * 10 + 18_000 * 0.1) / 1e6)


def test_haiku_costs_more_above_its_prompt_threshold(settings):
    haiku = settings.get("claude-haiku-5-5")
    assert haiku.cost(Usage(input_tokens=100_000, output_tokens=1000)) == pytest.approx(
        (100_000 * 0.10 + 1000 * 0.50) / 1e6
    )
    assert haiku.cost(Usage(input_tokens=100_001, output_tokens=1000)) == pytest.approx(
        (100_001 * 0.50 + 1000 * 2.50) / 1e6
    )
    # Cached prompt tokens count towards the threshold too.
    assert haiku.cost(Usage(input_tokens=1, cache_read_tokens=100_000)) == pytest.approx(
        (1 * 0.50 + 100_000 * 0.05) / 1e6
    )


@pytest.mark.parametrize(
    "table",
    [
        {"models": []},
        {"models": [{"id": "m", "label": "M", "levels": []}]},
        {"models": [{"id": "m", "label": "M", "levels": ["off", "low"]}]},
        {"models": [{"id": "m", "label": "M", "levels": ["extreme"]}]},
        {"default_model": "other", "models": [{"id": "m", "label": "M", "levels": ["low"]}]},
    ],
)
def test_invalid_settings_are_rejected(table):
    with pytest.raises(ValueError):
        parse_model_settings(table)


def test_broken_file_is_reported_with_its_path(tmp_path):
    file = tmp_path / "models.toml"
    file.write_text("default_model = ")
    with pytest.raises(ModelSettingsError, match="models.toml"):
        load_model_settings(file)


def test_api_key_comes_from_the_environment_first_then_the_keychain(keychain, monkeypatch):
    assert models.get_api_key() is None
    models.set_api_key("sk-from-keychain")
    assert models.get_api_key() == "sk-from-keychain"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-from-env")
    assert models.get_api_key() == "sk-from-env"


def test_a_failing_keychain_means_no_key(keychain, monkeypatch):
    def fail(service, user):
        raise models.keyring.errors.KeyringError("locked")

    monkeypatch.setattr(models.keyring, "get_password", fail)
    assert models.get_api_key() is None


def test_tests_never_reach_the_real_keychain(keychain):
    """Guards the fixture in conftest.py: the keychain is the in-memory one."""
    assert type(models.keyring.get_keyring()).__name__ == "InMemoryKeyring"
    assert models.get_api_key() is None
    models.set_api_key("sk-test")
    assert keychain == {("loccit", "anthropic-api-key"): "sk-test"}
