"""The models offered for checking, read from `~/.loccit/models.toml`.

The file is written with defaults on first use and can be edited by hand:
model ids, prices and the thinking levels each model accepts change over
time and should not need a code change.
"""

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

import keyring
import keyring.errors

from loccit.strategies import Usage

OFF = "off"
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
# The effort sent along when thinking is off.
OFF_EFFORT = "low"

_KEYRING_SERVICE = "loccit"
_KEYRING_USER = "anthropic-api-key"

# Model ids, prices and thinking behaviour as documented on 2026-11-17:
# https://platform.claude.com/docs/en/models/overview
# https://platform.claude.com/docs/en/build-with-claude/effort
DEFAULT_MODELS_TOML = """\
# Models loccit offers for checking citations. Edit freely; loccit reads this
# file at every start. `python -m loccit check-models` compares it with what
# the API reports for your key.
#
# levels: the thinking levels offered, in this order; the first one is the
#   default. "off" needs `thinking_off`, the value the model accepts for
#   thinking.type to respond without thinking first. Models whose thinking is
#   always on have no "off".
# Prices are US dollars per million tokens.

default_model = "claude-sonnet-5-5"

[[models]]
id = "claude-sonnet-5-5"
label = "Sonnet 5.5"
levels = ["off", "low", "medium", "high", "xhigh", "max"]
thinking_off = "between_tools"
input_per_mtok = 2.00
output_per_mtok = 10.00
cache_read_per_mtok = 0.10
cache_write_per_mtok = 2.50

[[models]]
id = "claude-opus-5-5"
label = "Opus 5.5"
levels = ["low", "medium", "high", "xhigh", "max"]
input_per_mtok = 4.00
output_per_mtok = 20.00
cache_read_per_mtok = 0.20
cache_write_per_mtok = 5.00

[[models]]
id = "claude-haiku-5-5"
label = "Haiku 5.5"
levels = ["off", "low", "medium", "high", "xhigh", "max"]
thinking_off = "disabled"
input_per_mtok = 0.10
output_per_mtok = 0.50
cache_read_per_mtok = 0.01
cache_write_per_mtok = 0.125
# Above this many prompt tokens the higher prices apply.
long_prompt_tokens = 100000
long_input_per_mtok = 0.50
long_output_per_mtok = 2.50

[[models]]
id = "claude-fable-5-2"
label = "Fable 5.2"
levels = ["low", "medium", "high", "xhigh", "max"]
input_per_mtok = 8.00
output_per_mtok = 40.00
cache_read_per_mtok = 0.20
cache_write_per_mtok = 10.00
"""


class ModelSettingsError(Exception):
    pass


@dataclass(frozen=True)
class ModelConfig:
    id: str
    label: str
    levels: tuple[str, ...]
    thinking_off: str | None = None
    input_per_mtok: float = 0.0
    output_per_mtok: float = 0.0
    cache_read_per_mtok: float | None = None
    cache_write_per_mtok: float | None = None
    long_prompt_tokens: int | None = None
    long_input_per_mtok: float | None = None
    long_output_per_mtok: float | None = None

    def request_options(self, level: str) -> dict:
        """The `thinking` and `output_config` parameters for a thinking level."""
        if level not in self.levels:
            raise ModelSettingsError(f"{self.id} does not offer thinking level {level!r}")
        if level == OFF:
            return {
                "thinking": {"type": self.thinking_off},
                "output_config": {"effort": OFF_EFFORT},
            }
        return {"thinking": {"type": "adaptive"}, "output_config": {"effort": level}}

    def cost(self, usage: Usage) -> float:
        """Estimated cost of a request in US dollars."""
        prompt_tokens = usage.input_tokens + usage.cache_read_tokens + usage.cache_write_tokens
        input_price, output_price, scale = self.input_per_mtok, self.output_per_mtok, 1.0
        if self.long_prompt_tokens is not None and prompt_tokens > self.long_prompt_tokens:
            input_price = self.long_input_per_mtok or input_price
            output_price = self.long_output_per_mtok or output_price
            # Cache prices rise by the same factor as the input price.
            scale = input_price / self.input_per_mtok if self.input_per_mtok else 1.0
        cache_read = self.cache_read_per_mtok
        cache_write = self.cache_write_per_mtok
        total = (
            usage.input_tokens * input_price
            + usage.output_tokens * output_price
            + usage.cache_read_tokens * (input_price if cache_read is None else cache_read * scale)
            + usage.cache_write_tokens
            * (input_price if cache_write is None else cache_write * scale)
        )
        return total / 1_000_000


@dataclass(frozen=True)
class ModelSettings:
    default_model: str
    models: tuple[ModelConfig, ...]

    def get(self, model_id: str) -> ModelConfig | None:
        return next((model for model in self.models if model.id == model_id), None)


def load_model_settings(path: Path) -> ModelSettings:
    """Read models.toml, writing it with the defaults first if it is missing."""
    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(DEFAULT_MODELS_TOML, encoding="utf-8")
    try:
        with path.open("rb") as file:
            return parse_model_settings(tomllib.load(file))
    except (tomllib.TOMLDecodeError, KeyError, TypeError, ValueError) as error:
        raise ModelSettingsError(f"{path}: {error}") from error


def parse_model_settings(table: dict) -> ModelSettings:
    models = []
    for entry in table["models"]:
        levels = tuple(entry["levels"])
        unknown = [level for level in levels if level != OFF and level not in EFFORT_LEVELS]
        if unknown or not levels:
            raise ValueError(f"model {entry['id']}: invalid levels {list(levels)}")
        if OFF in levels and not entry.get("thinking_off"):
            raise ValueError(f'model {entry["id"]}: level "off" needs thinking_off')
        models.append(ModelConfig(**{**entry, "levels": levels}))
    if not models:
        raise ValueError("no models listed")
    default_model = table.get("default_model", models[0].id)
    if default_model not in {model.id for model in models}:
        raise ValueError(f"default_model {default_model!r} is not among the models")
    return ModelSettings(default_model, tuple(models))


# --- API key ---------------------------------------------------------------


def get_api_key() -> str | None:
    """The Anthropic API key: the environment variable first, then the
    system keychain. The key is never written to a file or a log."""
    if key := os.environ.get("ANTHROPIC_API_KEY"):
        return key
    try:
        return keyring.get_password(_KEYRING_SERVICE, _KEYRING_USER)
    except keyring.errors.KeyringError:
        return None


def set_api_key(key: str) -> None:
    keyring.set_password(_KEYRING_SERVICE, _KEYRING_USER, key)


def delete_api_key() -> bool:
    try:
        keyring.delete_password(_KEYRING_SERVICE, _KEYRING_USER)
    except keyring.errors.PasswordDeleteError:
        return False
    return True
