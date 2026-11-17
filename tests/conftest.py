import keyring
import keyring.backend
import keyring.errors
import pytest


class InMemoryKeyring(keyring.backend.KeyringBackend):
    """A keychain that lives only as long as one test."""

    priority = 1

    def __init__(self):
        super().__init__()
        self.passwords: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.passwords.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.passwords[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        if self.passwords.pop((service, username), None) is None:
            raise keyring.errors.PasswordDeleteError("no such password")


@pytest.fixture(autouse=True)
def keychain(monkeypatch):
    """No test may read or write the real system keychain, or see an API key
    from the environment. The fixture's value is what the fake keychain holds."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    previous = keyring.get_keyring()
    backend = InMemoryKeyring()
    keyring.set_keyring(backend)
    yield backend.passwords
    keyring.set_keyring(previous)
